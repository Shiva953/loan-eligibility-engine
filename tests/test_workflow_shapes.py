"""Structural rules for the workflow templates that tests of single nodes can't catch.

A sub-workflow hands its result back on the output index of the last node it ran (n8n's
Execute Workflow node passes the sub-workflow's output arrays through). If a path ends on an
IF node's false branch, a loop's second output or an error output, the caller receives its
data on output 2, which has no connection, and the caller's own flow silently stops there:
Workflow B then matched only the first of several waiting batches (docs/DECISIONS.md V34).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.envfile import REPO_ROOT

SRC = REPO_ROOT / "n8n" / "src"
# Workflows that other workflows call and then continue after.
SUB_WORKFLOWS = ["c_notification.json", "llm_gateway.json", "warm_match_cache.json"]
TWO_OUTPUTS = {"n8n-nodes-base.if", "n8n-nodes-base.splitInBatches"}


def output_count(node: dict[str, Any]) -> int:
    """How many main outputs a node has, counting the error output when it is enabled."""
    base = 2 if node["type"] in TWO_OUTPUTS else 1
    return base + (1 if node.get("onError") == "continueErrorOutput" else 0)


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(SRC / name).read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("name", SUB_WORKFLOWS)
def test_no_path_of_a_sub_workflow_ends_on_a_second_output(name: str) -> None:
    workflow = load(name)
    loose = []
    for node in workflow["nodes"]:
        if node["type"].endswith("stickyNote"):
            continue
        outputs = workflow["connections"].get(node["name"], {}).get("main", [])
        for index in range(1, output_count(node)):
            if index >= len(outputs) or not outputs[index]:
                loose.append(f"{node['name']} output {index + 1}")
    assert loose == [], f"{name}: these outputs end the run off the first output: {loose}"


def test_workflow_b_continues_after_workflow_c() -> None:
    workflow = load("b_user_matching.json")
    after_c = workflow["connections"]["Notify users (Workflow C)"]["main"][0]
    assert [c["node"] for c in after_c] == ["Next batch"]


@pytest.mark.parametrize(
    ("name", "node"),
    [
        ("a_product_discovery.json", "Deactivate stale products"),
        ("a_product_discovery.json", "Crawl summary"),
        ("warm_match_cache.json", "Find unjudged cohorts"),
        ("llm_gateway.json", "Load app_config"),
    ],
)
def test_once_per_run_statements_ignore_how_many_items_arrive(name: str, node: str) -> None:
    """These statements must run once per execution.

    A Postgres node runs its query once per input item. These follow a loop's "done" output (one
    item per source) or a trigger fed many items: without Execute Once, A's tail ran deactivate
    3 times, the summary 3 times (9 rows) and the warm query 9 times.
    """
    found = next(n for n in load(name)["nodes"] if n["name"] == node)
    assert found.get("executeOnce") is True
