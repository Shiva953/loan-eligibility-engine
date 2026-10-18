"""Pull workflows out of the running n8n and fail if they drift from ``n8n/workflows/``.

Why: the repo is the source of truth for workflows, but the n8n editor is where people change
them. This script makes that gap visible. It exports every workflow (current drafts, which is
what someone edits), writes them to ``build/n8n-export/`` for porting back into ``n8n/src``,
and exits 1 with a diff if any content differs from the built JSON.

Only content is compared: name, nodes, connections, settings and pinData. Fields n8n adds on
import (versionId, timestamps, counters, sharing) differ on every instance and mean nothing.
An import followed by an export was checked to give back identical content.

Usage:
    uv run python scripts/export_workflows.py
"""

import difflib
import json
import subprocess
import sys
from typing import Any

from scripts.build_workflows import OUT_DIR
from scripts.envfile import REPO_ROOT
from scripts.n8n_ids import WORKFLOWS

EXPORT_DIR = REPO_ROOT / "build" / "n8n-export"
COMPARED_KEYS = ("name", "nodes", "connections", "settings", "pinData")
EXPORT_CMD = (
    'f=$(mktemp) && n8n export:workflow --all --output="$f" >/dev/null 2>&1 '
    '&& cat "$f"; rc=$?; rm -f "$f"; exit $rc'
)


def fetch_exported() -> list[dict[str, Any]]:
    """Export every workflow from the running n8n container."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "n8n", "sh", "-c", EXPORT_CMD],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"export failed (is the stack up?): {result.stderr.strip()[:300]}")
    exported: list[dict[str, Any]] = json.loads(result.stdout)
    return exported


def normalize(workflow: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields that describe what the workflow does."""
    content = {key: workflow.get(key) for key in COMPARED_KEYS}
    content["pinData"] = content["pinData"] or {}
    return content


def pretty(value: object) -> list[str]:
    """Render JSON for a line diff."""
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True).splitlines()


def compare(
    built: dict[str, dict[str, Any]], exported: list[dict[str, Any]]
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Return human-readable drift findings and the exports keyed by output filename."""
    findings: list[str] = []
    stem_by_id = {wf_id: stem for stem, wf_id in WORKFLOWS.items()}
    by_file: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()

    for workflow in exported:
        wf_id = workflow.get("id", "")
        seen_ids.add(wf_id)
        stem = stem_by_id.get(wf_id)
        if stem is None:
            findings.append(f"workflow {wf_id} ({workflow.get('name')!r}) exists only in n8n")
            by_file[f"unmanaged-{wf_id}.json"] = workflow
            continue
        by_file[f"{stem}.json"] = workflow
        if not workflow.get("active"):
            findings.append(f"{stem}: not published in n8n")
        expected, actual = normalize(built[f"{stem}.json"]), normalize(workflow)
        if expected != actual:
            diff = difflib.unified_diff(
                pretty(expected), pretty(actual), "repo", "n8n", lineterm="", n=2
            )
            findings.append(f"{stem}: content differs\n" + "\n".join(list(diff)[:80]))

    for stem, wf_id in WORKFLOWS.items():
        if wf_id not in seen_ids:
            findings.append(f"{stem}: in the repo but missing from n8n (run `make bootstrap`)")
    return findings, by_file


def main() -> int:
    """Export, save copies, and report drift."""
    built = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in OUT_DIR.glob("*.json")}
    findings, by_file = compare(built, fetch_exported())

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for old in EXPORT_DIR.glob("*.json"):
        old.unlink()
    for name, workflow in by_file.items():
        (EXPORT_DIR / name).write_text(
            json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"exported {len(by_file)} workflows to {EXPORT_DIR.relative_to(REPO_ROOT)}")

    if findings:
        print("\nDRIFT between n8n and the repo:")
        for finding in findings:
            print(f"- {finding}")
        print("\nPort editor changes into n8n/src (and sql/, n8n/code/), then `make bootstrap`.")
        return 1
    print("no drift: n8n matches n8n/workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
