"""Measure where a matching run's time goes, with the network cost reported separately.

Why: this project's AWS resources live in ap-southeast-2 (Sydney), because the account's
simplified sign-up pins it there, while n8n runs on a laptop in Bengaluru (docs/DECISIONS.md
AD27). Every statement n8n sends to RDS then pays a Bengaluru-Sydney round trip, and so does the
tunnel hop from the notify Lambda to n8n. One end-to-end number would mix that distance with
the work itself, so this measures the round-trip time at runtime, counts the round trips each
workflow run makes, and reports the two apart:

* in-region work: loading the batch (the ingest Lambda's job in production) and SQL execution
  as the database server timed it (pipeline_metrics.duration_ms of the run's own statements);
* network: round trips x measured RTT, plus connection setup when n8n's pool is cold, plus the
  tunnel hop.

How each number is measured:
* region RTT: TCP handshakes with the regional STS endpoint (connect only: no request is sent,
  nothing is billed), and with ap-south-1 for comparison;
* database RTT on --target: `SELECT 1` on an open connection, and a fresh connection (TCP, TLS
  when the target uses it, authentication, `SELECT 1`);
* tunnel: unsigned POSTs to the webhook over one kept-alive connection, through the cloudflared
  URL and directly to webhook-gw (n8n rejects them before any workflow starts);
* round trips: Postgres node runs in the run's n8n executions (the Workflow B webhook execution
  and every sub-execution it started). The node sends one run's statements as a single message
  (DECISIONS V23), so one node run is one round trip.

Usage:
    make timing                                    # probes, plus the latest Workflow B run
    make timing ARGS="--run 10000"                 # load 10,000 fresh users and time that run
    make timing ARGS="--target rds --run 10000"    # after `make use-rds`
    make timing ARGS="--compare 144"               # also count another execution's round trips
"""

import argparse
import json
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any

import psycopg

from bench import benchdb
from scripts.dbconn import Target, admin_connect_kwargs
from scripts.envfile import REPO_ROOT, load_settings
from scripts.gen_users import generate
from scripts.match_e2e import send_webhook, wait_for
from scripts.n8n_execution import unflatten
from scripts.n8n_ids import WORKFLOWS
from scripts.n8n_sql import run_dicts
from scripts.tunnel_url import from_metrics

COMPARE_REGION = "ap-south-1"
# A fresh Postgres connection over TLS costs about five round trips: TCP handshake, SSLRequest,
# TLS 1.3 handshake, and two SCRAM exchanges (the last one also carries ReadyForQuery).
FRESH_CONNECTION_RTTS = 5
# n8n's pg pool closes a connection idle for 10 s (node-postgres default; the credential can't
# change it), so a run that starts after a quiet spell opens one fresh connection (DECISIONS V32).
POOL_IDLE_SECONDS = 10


@dataclass
class Sample:
    """Median and p90 of repeated timings, in milliseconds."""

    what: str
    median_ms: float | None
    p90_ms: float | None
    n: int
    note: str = ""

    @classmethod
    def of(cls, what: str, values: list[float], note: str = "") -> Sample:
        """Summarise raw timings (an empty list means the probe could not run)."""
        if not values:
            return cls(what, None, None, 0, note)
        ordered = sorted(values)
        p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
        return cls(what, round(statistics.median(ordered), 2), round(p90, 2), len(values), note)

    def line(self) -> str:
        """One aligned report line."""
        if self.median_ms is None:
            return f"  {self.what:44} {self.note}"
        extra = f"  ({self.note})" if self.note else ""
        return (
            f"  {self.what:44} median {self.median_ms:8.2f} ms  p90 {self.p90_ms:8.2f} ms"
            f"  n={self.n}{extra}"
        )


@dataclass
class Execution:
    """What one n8n execution did, from its stored run data."""

    id: str
    workflow: str
    mode: str
    duration_ms: int
    round_trips: dict[str, int] = field(default_factory=dict)
    postgres_ms: int = 0
    llm_calls: int = 0
    ses_sends: int = 0
    children: list[str] = field(default_factory=list)


# --- probes --------------------------------------------------------------------------------------


def tcp_rtt(host: str, samples: int) -> Sample:
    """TCP handshake time to host:443 (DNS resolved once, outside the timing)."""
    try:
        ip = str(socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])
    except OSError as error:
        return Sample.of(host, [], f"unresolved: {error}")
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        try:
            with socket.create_connection((ip, 443), timeout=5):
                values.append((time.perf_counter() - start) * 1000)
        except OSError:
            continue
    return Sample.of(f"TCP handshake {host}", values, ip)


def db_rtt(kwargs: dict[str, Any], samples: int, fresh: int) -> tuple[Sample, Sample]:
    """`SELECT 1` on an open connection, and a whole fresh connection plus `SELECT 1`."""
    open_values, fresh_values = [], []
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        conn.execute("SELECT 1").fetchone()  # warm-up
        for _ in range(samples):
            start = time.perf_counter()
            conn.execute("SELECT 1").fetchone()
            open_values.append((time.perf_counter() - start) * 1000)
        tls = conn.pgconn.ssl_in_use
    for _ in range(fresh):
        start = time.perf_counter()
        with psycopg.connect(**kwargs, autocommit=True) as conn:
            conn.execute("SELECT 1").fetchone()
        fresh_values.append((time.perf_counter() - start) * 1000)
    return (
        Sample.of("SELECT 1 on an open connection", open_values),
        Sample.of("fresh connection + SELECT 1", fresh_values, "TLS" if tls else "no TLS"),
    )


def http_rtt(base_url: str, samples: int) -> Sample:
    """Unsigned POSTs to the webhook on one kept-alive connection (the first one is not timed)."""
    url = urllib.parse.urlsplit(base_url)
    conn_class = HTTPSConnection if url.scheme == "https" else HTTPConnection
    conn = conn_class(url.netloc, timeout=10)
    values: list[float] = []
    status = None
    try:
        for i in range(samples + 1):
            start = time.perf_counter()
            conn.request("POST", "/webhook/loan-match", body=b"{}",
                         headers={"Content-Type": "application/json"})  # fmt: skip
            response = conn.getresponse()
            response.read()
            status = response.status
            if i:
                values.append((time.perf_counter() - start) * 1000)
    except OSError as error:
        return Sample.of(base_url, [], f"unreachable: {error}")
    finally:
        conn.close()
    return Sample.of(f"POST {url.netloc}", values, f"HTTP {status}, rejected unsigned")


# --- n8n executions -------------------------------------------------------------------------------


def n8n_query(sql: str) -> list[list[str]]:
    """Rows from n8n's own database, as text."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "n8n-db", "psql", "-U", "n8n", "-d", "n8n",
         "-At", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    )  # fmt: skip
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def latest_execution_id(workflow_id: str, mode: str | None = None) -> int:
    """The newest execution id of a workflow (0 when it never ran)."""
    where = f"\"workflowId\" = '{workflow_id}'" + (f" AND mode = '{mode}'" if mode else "")
    rows = n8n_query(f"SELECT coalesce(max(id), 0) FROM execution_entity WHERE {where}")
    return int(rows[0][0])


def wait_for_execution(workflow_id: str, mode: str, after: int, timeout: float) -> int:
    """The first finished execution of a workflow, in one mode, with an id above `after`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = n8n_query(
            f"SELECT id FROM execution_entity WHERE \"workflowId\" = '{workflow_id}' "
            f"AND mode = '{mode}' AND id > {after} "
            "AND (finished OR status IN ('error', 'crashed', 'success')) ORDER BY id LIMIT 1"
        )
        if rows:
            return int(rows[0][0])
        time.sleep(0.25)
    raise SystemExit(f"no finished execution of {workflow_id} after id {after}")


def load_execution(execution_id: str) -> Execution:
    """Decode one execution and count its round trips, LLM calls and SES sends."""
    rows = n8n_query(
        'SELECT e.id, e."workflowId", e.mode, '
        'round(extract(epoch FROM e."stoppedAt" - e."startedAt") * 1000), '
        'd."workflowData", d.data FROM execution_entity e '
        f'JOIN execution_data d ON d."executionId" = e.id WHERE e.id = {int(execution_id)}'
    )
    if not rows:
        raise SystemExit(f"execution {execution_id} not found")
    exec_id, workflow_id, mode, duration, workflow_json, data_json = rows[0]
    workflow = json.loads(workflow_json)
    types = {node["name"]: node["type"] for node in workflow["nodes"]}
    run_data = unflatten(json.loads(data_json))["resultData"]["runData"]
    name = workflow.get("name", workflow_id)
    execution = Execution(exec_id, name, mode, int(float(duration or 0)))
    gateway = workflow_id == WORKFLOWS["llm_gateway"]
    for node, runs in run_data.items():
        node_type = types.get(node, "")
        for run in runs:
            if node_type == "n8n-nodes-base.postgres":
                execution.round_trips[node] = execution.round_trips.get(node, 0) + 1
                execution.postgres_ms += int(run.get("executionTime") or 0)
            elif node_type == "n8n-nodes-base.httpRequest" and gateway:
                execution.llm_calls += 1
            elif node_type == "n8n-nodes-base.awsSes":
                execution.ses_sends += 1
            metadata = run.get("metadata") or {}
            subs = metadata.get("subExecutions") or (
                [metadata["subExecution"]] if metadata.get("subExecution") else []
            )
            execution.children += [str(s["executionId"]) for s in subs if s.get("executionId")]
    return execution


def execution_tree(root_id: str) -> list[Execution]:
    """An execution and every sub-execution it started, depth first."""
    root = load_execution(root_id)
    out = [root]
    for child in root.children:
        out += execution_tree(child)
    return out


def db_state(conn: psycopg.Connection) -> dict[str, Any]:
    """How much data the database holds: insert cost grows with the size of the match index."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM users), "
        "(SELECT coalesce(sum(cardinality(product_ids)), 0) FROM user_matches), "
        "pg_size_pretty(pg_total_relation_size('user_matches')), current_setting('shared_buffers')"
    ).fetchone()
    assert row
    return {"users": row[0], "matches": row[1], "matches_size": row[2], "shared_buffers": row[3]}


def server_sql_ms(conn: psycopg.Connection, run_ids: list[str]) -> tuple[int, int]:
    """SQL execution time as the server measured it: (milliseconds, statements).

    Rows written by one statement share created_at (the transaction start), so each statement
    counts once even when it writes several stage rows with the same duration.
    """
    row = conn.execute(
        "SELECT coalesce(sum(ms), 0), count(*) FROM ("
        "  SELECT max(duration_ms) AS ms FROM pipeline_metrics"
        "  WHERE run_id = ANY(%s) AND duration_ms IS NOT NULL GROUP BY run_id, created_at) s",
        (run_ids,),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


# --- a fresh run ---------------------------------------------------------------------------------


def would_call_llm(conn: psycopg.Connection, users: list[dict[str, Any]]) -> int:
    """LLM requests' worth of work Workflow B would find for this batch (0 when warm).

    Runs the production claim-and-find statement without claiming, inside a transaction that is
    rolled back, so nothing is kept: not the users, not the metrics.
    """
    with conn.transaction(force_rollback=True):
        batch_id = benchdb.load_batch(conn, users, "timing-dry-run")
        found = run_dicts(conn, "match/uncached_cohorts.sql", batch_id, "", "", "dry", "", "false")
    return int(found[0]["cohorts_uncached"]) + int(found[0]["criteria_uncompiled"])


def fresh_run(
    conn: psycopg.Connection, settings: dict[str, str], n: int, url: str | None, allow_llm: bool
) -> tuple[str, float, float, str]:
    """Load n new users, send the signed webhook, wait for Workflow C to close the batch.

    Returns (batch id, ingest seconds, webhook-to-notified seconds, Workflow B execution id).
    """
    mode = conn.execute("SELECT value FROM app_config WHERE key = 'email_mode'").fetchone()
    if not mode or mode[0] != "suppressed":
        raise SystemExit(
            "refusing to start a run: app_config email_mode is not 'suppressed', and a timed "
            "run must never send email"
        )
    users = generate(n, int(time.time()), "real")
    if (pending := would_call_llm(conn, users)) and not allow_llm:
        raise SystemExit(
            f"refusing to start a run: this batch would need {pending} cohort(s) or rule(s) "
            "judged by the LLM. Run `make crawl` first (it warms the cache), or pass --allow-llm."
        )
    started = time.perf_counter()
    batch_id = benchdb.load_batch(conn, users, f"timing-{int(time.time())}")
    ingest_s = time.perf_counter() - started
    before = latest_execution_id(WORKFLOWS["b_user_matching"], "webhook")
    sent = time.perf_counter()
    status = send_webhook(batch_id, settings, url)
    if status != 202:
        raise SystemExit(f"webhook returned HTTP {status}")
    final = wait_for(conn, batch_id, {"notified", "failed"}, 600)
    wall_s = time.perf_counter() - sent
    if final != "notified":
        raise SystemExit(f"batch {batch_id} ended {final}")
    exec_id = wait_for_execution(WORKFLOWS["b_user_matching"], "webhook", before, 60)
    return batch_id, ingest_s, wall_s, str(exec_id)


# --- report --------------------------------------------------------------------------------------


def ms(value: float | None) -> str:
    """Milliseconds for the report, or n/a."""
    return "n/a" if value is None else f"{value:,.0f} ms"


def summarise(tree: list[Execution]) -> dict[str, Any]:
    """Round trips and time per workflow, and totals for the tree."""
    return {
        "executions": [asdict(e) for e in tree],
        "round_trips": sum(sum(e.round_trips.values()) for e in tree),
        "postgres_node_ms": sum(e.postgres_ms for e in tree),
        "llm_calls": sum(e.llm_calls for e in tree),
        "ses_sends": sum(e.ses_sends for e in tree),
        "duration_ms": tree[0].duration_ms if tree else 0,
    }


def print_tree(title: str, tree: list[Execution]) -> None:
    """One line per execution: duration, round trips by node, LLM calls, SES sends."""
    print(f"\n{title}")
    for e in tree:
        trips = ", ".join(f"{k} {v}" for k, v in e.round_trips.items()) or "none"
        extra = "".join(
            [f", LLM calls {e.llm_calls}" if e.llm_calls else "",
             f", SES sends {e.ses_sends}" if e.ses_sends else ""]
        )  # fmt: skip
        print(f"  {e.workflow[:34]:34} exec {e.id:>6} {e.duration_ms:>7,} ms  "
              f"round trips {sum(e.round_trips.values())} ({trips}){extra}")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    """Probe, optionally run a batch, and print the split between work and distance."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local",
                        help="database for the probes and --run (default: local)")  # fmt: skip
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--fresh", type=int, default=5, help="fresh connections to time")
    parser.add_argument("--run", type=int, metavar="N", help="time a fresh batch of N users")
    parser.add_argument("--url", help="send --run's webhook through this base URL")
    parser.add_argument("--allow-llm", action="store_true",
                        help="let --run go ahead even if the batch needs LLM calls")  # fmt: skip
    parser.add_argument("--compare", metavar="EXEC", help="also count this B execution's trips")
    parser.add_argument("--json", type=Path, help="default bench/results/timing-<target>.json")
    args = parser.parse_args(argv)

    settings = load_settings()
    target: Target = args.target
    region = settings.get("AWS_REGION", "")
    report: dict[str, Any] = {"target": target, "region": region,
                              "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}  # fmt: skip

    print(f"Network, measured now from this machine (AWS_REGION={region})")
    region_rtt = tcp_rtt(f"sts.{region}.amazonaws.com", args.samples)
    compare_rtt = tcp_rtt(f"sts.{COMPARE_REGION}.amazonaws.com", args.samples)
    print(region_rtt.line())
    print(compare_rtt.line())

    try:
        kwargs = admin_connect_kwargs(settings, target)
        open_rtt, fresh_rtt = db_rtt(kwargs, args.samples, args.fresh)
    except (SystemExit, psycopg.OperationalError) as error:
        reason = str(error).splitlines()[0] if str(error) else "not configured"
        open_rtt = Sample.of(f"database ({target})", [], f"not reachable: {reason}")
        fresh_rtt = Sample.of("fresh connection", [], "")
    print(f"  database: {target}")
    print(open_rtt.line())
    if fresh_rtt.n:
        print(fresh_rtt.line())

    direct = http_rtt(f"http://127.0.0.1:{settings.get('WEBHOOK_GW_HOST_PORT', '8088')}", 10)
    tunnel_url = from_metrics(settings.get("CLOUDFLARED_METRICS_HOST_PORT", "20241"))
    tunnel = (
        http_rtt(tunnel_url, 10)
        if tunnel_url
        else Sample.of("tunnel", [], "not running (`make tunnel` to include it)")
    )
    print(direct.line())
    print(tunnel.line())
    report["network"] = {
        "region_tcp": asdict(region_rtt), "compare_region_tcp": asdict(compare_rtt),
        "db_select1": asdict(open_rtt), "db_fresh_connection": asdict(fresh_rtt),
        "webhook_direct": asdict(direct), "webhook_tunnel": asdict(tunnel),
    }  # fmt: skip

    ingest_s = wall_s = None
    batch_id = None
    with psycopg.connect(**admin_connect_kwargs(settings, target), autocommit=True) as conn:
        if args.run:
            batch_id, ingest_s, wall_s, b_exec = fresh_run(
                conn, settings, args.run, args.url, args.allow_llm
            )
        else:
            b_exec = str(latest_execution_id(WORKFLOWS["b_user_matching"], "webhook"))
            if b_exec == "0":
                raise SystemExit("no Workflow B webhook run yet: try --run 10000")
        tree = execution_tree(b_exec)
        sql_ms, statements = server_sql_ms(conn, [e.id for e in tree])
        report["database"] = db_state(conn)

    b = summarise(tree)
    title = f"Workflow B run (execution {b_exec}" + (
        f", batch {batch_id}, {args.run:,} fresh users)" if batch_id else ", latest)"
    )
    print_tree(title, tree)
    data = report["database"]
    print(f"  database holds {data['users']:,} users and {data['matches']:,} matches "
          f"({data['matches_size']}; shared_buffers {data['shared_buffers']})")  # fmt: skip
    if args.compare:
        before_tree = execution_tree(args.compare)
        print_tree(f"For comparison: execution {args.compare}", before_tree)
        report["compare"] = summarise(before_tree)
    a_exec = latest_execution_id(WORKFLOWS["a_product_discovery"])
    a_tree = execution_tree(str(a_exec)) if a_exec else []
    if a_tree:
        print_tree(f"Workflow A, latest run (execution {a_exec}, for its round trips)", a_tree)

    trips = b["round_trips"]
    rtt = region_rtt.median_ms
    db_now = open_rtt.median_ms or 0.0
    remote = target == "rds" and open_rtt.median_ms is not None
    per_trip = open_rtt.median_ms if remote else rtt
    cold = fresh_rtt.median_ms if remote else (rtt * FRESH_CONNECTION_RTTS if rtt else None)
    overhead = (
        max(0.0, tunnel.median_ms - direct.median_ms)
        if tunnel.median_ms is not None and direct.median_ms is not None
        else None
    )
    tunnel_hop = rtt + (overhead or 0.0) if rtt is not None else None
    network_ms = (trips * per_trip) if per_trip is not None else None
    split = {
        "ingest_ms": round(ingest_s * 1000) if ingest_s is not None else None,
        "sql_server_ms": sql_ms,
        "sql_statements_timed": statements,
        "postgres_node_ms": b["postgres_node_ms"],
        "n8n_other_ms": b["duration_ms"] - b["postgres_node_ms"],
        "workflow_b_ms": b["duration_ms"],
        "webhook_to_notified_ms": round(wall_s * 1000) if wall_s is not None else None,
        "round_trips": trips,
        "network_now_ms": round(trips * db_now, 1),
        "cross_region_per_trip_ms": per_trip,
        "cross_region_round_trips_ms": round(network_ms) if network_ms is not None else None,
        "cold_pool_connection_ms": round(cold) if cold is not None else None,
        "tunnel_overhead_ms": overhead,
        "tunnel_hop_ms": round(tunnel_hop) if tunnel_hop is not None else None,
        "ses_sends": b["ses_sends"],
        "llm_calls": b["llm_calls"],
    }
    report["workflow_b"] = b
    report["workflow_a"] = summarise(a_tree) if a_tree else None
    report["split"] = split

    basis = "measured on RDS" if remote else f"{region} TCP RTT, measured above"
    print("\nWhere the time goes")
    print(f"  in-region work     ingest (batch load)            {ms(split['ingest_ms'])}")
    print(f"                     SQL execution on the server    {ms(sql_ms)} "
          f"({statements} timed statement(s))")  # fmt: skip
    print(f"  n8n on this laptop Code nodes, scheduling, calls  {ms(split['n8n_other_ms'])}")
    print(f"  network now        {trips} round trip(s) x {db_now:.2f} ms to {target}"
          f"        {split['network_now_ms']} ms")  # fmt: skip
    print(f"  cross-region       {trips} round trip(s) x {ms(per_trip)} ({basis})"
          f" = {ms(network_ms)}")  # fmt: skip
    print(f"   (n8n here,        + one fresh connection if the pool sat idle > "
          f"{POOL_IDLE_SECONDS} s: {ms(cold)}")  # fmt: skip
    print(
        f"    RDS in {region}) + tunnel hop, notify Lambda -> n8n: {ms(tunnel_hop)}"
        + (" (region RTT + tunnel overhead)" if overhead is not None else " (region RTT)")
    )
    if b["ses_sends"]:
        print(f"                     + {b['ses_sends']} SES call(s), each about 3 x region RTT")
    print(
        f"  in-region fix      the same {trips} round trip(s) at about 1 ms each (n8n next to RDS)"
    )
    if split["webhook_to_notified_ms"] is not None:
        print(f"\n  measured webhook -> notified: {ms(split['webhook_to_notified_ms'])} "
              f"(Workflow B {ms(b['duration_ms'])}), LLM calls {b['llm_calls']}")  # fmt: skip

    out = args.json or REPO_ROOT / "bench" / "results" / f"timing-{target}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shown = out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out
    print(f"\nreport written to {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
