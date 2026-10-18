"""One Postgres connection per Lambda container, reused by every invocation it serves.

Why reuse: a fresh connection costs TCP, TLS and authentication on each request; a warm
container keeps its connection open between invocations. In the VPC the password is an RDS IAM
auth token signed offline (common.sigv4; docs/DECISIONS.md AD1), so no secret is stored and no
network call is needed to get it. DB_AUTH=password (local runs) uses DB_PASSWORD instead.

Why logins are hedged, never cancelled (docs/DECISIONS.md AD41): inside the VPC a network round
trip takes about 1 ms, so a login's time is almost all RDS's server-side IAM check, and the
first check from a fresh container has taken over 10 s. Cancelling a slow login and starting
again restarts that check, so a ladder of short attempts could fail every time (it did:
3, 5 and 10 s attempts, then Lambda's own retry 64 s later). So the first attempt waits for the
whole budget, and further attempts start beside it after a few seconds, in case its path is
stuck rather than slow. Whichever finishes first is used; the others close when they finish.
"""

import os
import queue
import socket
import sys
import threading
import time
from typing import Any

import psycopg
from psycopg import pq

from common.sigv4 import Credentials, rds_auth_token

_conn: psycopg.Connection | None = None
# Seconds after the first attempt when attempts 2, 3, ... start beside the ones still waiting.
HEDGE_AFTER = (4.0, 10.0, 20.0, 35.0)
# Pause before a new attempt when every attempt so far failed fast (e.g. RDS still starting).
RETRY_PAUSE = 1.0
DEFAULT_BUDGET = 60.0


def _log(message: str) -> None:
    print(f"db: {message}", file=sys.stderr, flush=True)


def budget_for(context: object, reserve: float, cap: float) -> float:
    """Seconds a handler may spend logging in: its remaining time minus `reserve`, at most `cap`.

    `context` is the Lambda context (None outside Lambda, e.g. local runs and tests).
    """
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if remaining is None:
        return min(cap, DEFAULT_BUDGET)
    return max(5.0, min(cap, float(remaining()) / 1000 - reserve))


def _diagnose(host: str, port: int) -> str:
    """Where a failed login stops: the address the name resolves to, and a raw TCP try."""
    try:
        addresses = sorted(
            {str(a[4][0]) for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        )
    except OSError as error:
        return f"DNS failed: {error}"
    started = time.perf_counter()
    try:
        with socket.create_connection((addresses[0], port), timeout=2):
            tcp = f"TCP ok in {(time.perf_counter() - started) * 1000:.0f} ms"
    except OSError as error:
        tcp = f"TCP failed after {(time.perf_counter() - started) * 1000:.0f} ms: {error}"
    return f"{host} -> {', '.join(addresses)}; {tcp}"


def _params() -> dict[str, Any]:
    env = os.environ
    host, port, user = env["DB_HOST"], int(env.get("DB_PORT", "5432")), env["DB_USER"]
    if env.get("DB_AUTH", "iam") == "password":
        password = env["DB_PASSWORD"]
    else:
        password = rds_auth_token(host=host, port=port, user=user, region=env["AWS_REGION"],
                                  credentials=Credentials.from_env())  # fmt: skip
    return {
        "host": host, "port": port, "dbname": env.get("DB_NAME", "loans"), "user": user,
        "password": password, "sslmode": env.get("DB_SSLMODE", "verify-full"),
        "sslrootcert": env.get("DB_SSLROOTCERT"),
        "application_name": env.get("AWS_LAMBDA_FUNCTION_NAME", "lambda"),
    }  # fmt: skip


def _alive(conn: psycopg.Connection | None) -> bool:
    """Is the cached connection usable? One round trip (about 1 ms in the VPC) checks.

    A server restart or an idle cut leaves the connection open on our side only, and the first
    query would then fail the invocation.
    """
    if conn is None or conn.closed or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        return False
    try:
        conn.execute("SELECT 1")
        conn.rollback()
    except psycopg.Error:
        conn.close()
        _log("the cached connection was dead; logging in again")
        return False
    return True


def _hedged_login(params: dict[str, Any], budget: float) -> psycopg.Connection:
    """The first attempt to log in within `budget` seconds, with hedged attempts beside it."""
    start = time.monotonic()
    deadline = start + budget
    results: queue.Queue[tuple[int, psycopg.Connection | None, Exception | None, float]]
    results = queue.Queue()
    lock = threading.Lock()
    settled = [False]  # set once a winner is chosen or the budget is spent

    def attempt(number: int) -> None:
        began = time.monotonic()
        try:
            conn = psycopg.connect(**params, connect_timeout=max(2, int(deadline - began)))
        except Exception as error:  # reported to the caller through the queue
            results.put((number, None, error, time.monotonic() - began))
            return
        with lock:
            late = settled[0]
            settled[0] = True
        if late:
            conn.close()  # a faster attempt won, or the caller already gave up
            _log(f"attempt {number} finished after {time.monotonic() - began:.1f} s; not needed")
            return
        results.put((number, conn, None, time.monotonic() - began))

    launched = running = 0
    next_hedge = 0
    last_error: Exception | None = None

    def launch() -> None:
        nonlocal launched, running
        launched += 1
        running += 1
        threading.Thread(target=attempt, args=(launched,), daemon=True).start()

    launch()
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        hedge_at = start + HEDGE_AFTER[next_hedge] if next_hedge < len(HEDGE_AFTER) else deadline
        try:
            number, conn, error, took = results.get(timeout=max(0.0, min(hedge_at, deadline) - now))
        except queue.Empty:
            if next_hedge < len(HEDGE_AFTER) and time.monotonic() < deadline:
                next_hedge += 1
                _log(f"login still waiting after {time.monotonic() - start:.1f} s; starting "
                     f"attempt {launched + 1} beside it")  # fmt: skip
                launch()
            continue
        running -= 1
        if conn is not None:
            total = time.monotonic() - start
            if number > 1 or total > 2:
                _log(f"logged in by attempt {number} of {launched} after {total:.1f} s")
            return conn
        last_error = error
        _log(f"attempt {number} failed after {took:.1f} s ({error}); "
             f"{_diagnose(params['host'], params['port'])}")  # fmt: skip
        if running == 0 and time.monotonic() + RETRY_PAUSE < deadline:
            time.sleep(RETRY_PAUSE)  # every attempt failed fast: try again shortly
            launch()
    with lock:
        settled[0] = True
    raise psycopg.OperationalError(
        f"no database login within {budget:.0f} s ({launched} attempts): {last_error}"
    )


def connect(budget: float = DEFAULT_BUDGET) -> psycopg.Connection:
    """The container's connection: reused while it works, otherwise a hedged login."""
    global _conn
    if _alive(_conn):
        assert _conn is not None
        return _conn
    _conn = _hedged_login(_params(), budget)
    return _conn


def warm(context: object, cap: float = 60.0) -> dict[str, Any]:
    """Log in now, off the user's path (the keep-warm schedule and post-deploy warm-up, AD41).

    Never raises: a stopped database must not turn a warm-up into retries, a dead-letter
    message or minutes of billed waiting. A quick TCP check comes first for the same reason.
    """
    started = time.perf_counter()
    if _alive(_conn):
        return {"warm": True, "reused": True, "ms": round((time.perf_counter() - started) * 1000)}
    host, port = os.environ["DB_HOST"], int(os.environ.get("DB_PORT", "5432"))
    try:
        socket.create_connection((host, port), timeout=3).close()
        connect(budget_for(context, reserve=5, cap=cap))
    except (OSError, psycopg.Error) as error:
        _log(f"warm-up skipped: {error}")
        return {"warm": False, "error": str(error)[:300]}
    return {"warm": True, "reused": False, "ms": round((time.perf_counter() - started) * 1000)}
