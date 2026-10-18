"""Inspect a stored n8n execution: per-node runs, item counts and errors, or one node's output.

Why: n8n keeps execution data in its own database in the "flatted" format (a JSON array whose
strings are back-references), which no SQL query can read. This decodes it for two jobs:
debugging a run without the UI, and the bench's "max items held in any node" metric
(docs/PLAN.md phase 4), measured from what n8n actually stored.

Usage:
    uv run python scripts/n8n_execution.py                       # latest execution, all workflows
    uv run python scripts/n8n_execution.py --workflow lnWfProductDisc1
    uv run python scripts/n8n_execution.py --node "Validate products" --run 0
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


def unflatten(flat: list[Any]) -> Any:
    """Decode the flatted format: every string inside a container is an index into ``flat``."""
    cache: dict[int, Any] = {}

    def revive(index: int) -> Any:
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, list):
            out_list: list[Any] = []
            cache[index] = out_list
            out_list.extend(resolve(item) for item in value)
            return out_list
        if isinstance(value, dict):
            out_dict: dict[str, Any] = {}
            cache[index] = out_dict
            for key, item in value.items():
                out_dict[key] = resolve(item)
            return out_dict
        return value

    def resolve(item: Any) -> Any:
        return revive(int(item)) if isinstance(item, str) else item

    return revive(0)


@dataclass(frozen=True)
class NodeRun:
    """One run of one node: how many items it emitted per output and any error."""

    node: str
    run: int
    status: str
    items_per_output: list[int]
    error: str | None


def fetch_execution(
    workflow_id: str | None, execution_id: str | None, mode: str | None = None
) -> tuple[str, Any]:
    """Load one execution's decoded data from n8n-db (latest unless an id is given).

    mode narrows "latest" to one trigger kind, e.g. 'webhook' (the sweeper's runs are 'trigger').
    """
    if execution_id:
        where = f"e.id = {int(execution_id)}"
    elif workflow_id:
        where = f"e.\"workflowId\" = '{workflow_id.replace(chr(39), '')}'"
    else:
        where = "true"
    if mode and not execution_id:
        where += f" AND e.mode = '{mode.replace(chr(39), '')}'"
    sql = (
        "SELECT e.id, d.data FROM execution_entity e JOIN execution_data d "
        f'ON d."executionId" = e.id WHERE {where} ORDER BY e.id DESC LIMIT 1'
    )
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "n8n-db", "psql", "-U", "n8n", "-d", "n8n",
         "-At", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    if not result.stdout.strip():
        raise SystemExit("no matching execution")
    exec_id, data = result.stdout.rstrip("\n").split("\t", 1)
    return exec_id, unflatten(json.loads(data))


def node_runs(data: Any) -> list[NodeRun]:
    """Summarise every node run in an execution."""
    runs: list[NodeRun] = []
    for node, node_runs_ in data["resultData"]["runData"].items():
        for index, run in enumerate(node_runs_):
            outputs = (run.get("data") or {}).get("main") or []
            error = run.get("error")
            runs.append(
                NodeRun(
                    node=node,
                    run=index,
                    status=run.get("executionStatus", "?"),
                    items_per_output=[len(o or []) for o in outputs],
                    error=(error or {}).get("message") if error else None,
                )
            )
    return runs


def shorten(value: Any, limit: int = 300) -> Any:
    """Cut long strings (page bodies, prompts) so a dump stays readable and valid JSON."""
    if isinstance(value, str) and len(value) > limit:
        return f"{value[:limit]}… [{len(value):,} chars]"
    if isinstance(value, list):
        return [shorten(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: shorten(item, limit) for key, item in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    """Print a per-node summary, or dump one node run's output items."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--workflow", help="workflow id (default: any)")
    parser.add_argument("--execution", help="execution id (default: latest)")
    parser.add_argument("--node", help="dump this node's output items")
    parser.add_argument("--run", type=int, default=0, help="which run of --node (default 0)")
    args = parser.parse_args(argv)

    exec_id, data = fetch_execution(args.workflow, args.execution)
    if args.node:
        run = data["resultData"]["runData"][args.node][args.run]
        outputs = (run.get("data") or {}).get("main") or []
        print(json.dumps(shorten({"error": run.get("error"), "outputs": outputs}), indent=2))
        return 0

    error = data["resultData"].get("error")
    print(f"execution {exec_id}: {'ERROR ' + error.get('message', '') if error else 'ok'}")
    for r in node_runs(data):
        flag = f"  !! {r.error}" if r.error else ""
        print(f"  {r.node:28} run {r.run:<2} {r.status:8} items {r.items_per_output}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
