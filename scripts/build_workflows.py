"""Build importable n8n workflow JSON from ``n8n/src`` templates.

Why templates: the matching SQL and the Code-node JavaScript live in their own files
(``sql/``, ``n8n/code/``) so the bench can run exactly the same logic outside n8n, and so they
can be linted and diffed as code. Templates refer to them by placeholder, and this script
inlines them. The output in ``n8n/workflows/`` is a deliverable and is never edited by hand.

Placeholders, each the entire value of a JSON string:
    "@@include:<repo-relative path>"   -> that file's text (only under sql/ or n8n/code/)
    "@@credential:<key>"               -> {"id", "name"} from scripts/n8n_ids.py
    "@@workflow:<template stem>"       -> that workflow's ID (for Execute Workflow nodes)

Usage:
    uv run python scripts/build_workflows.py            # write n8n/workflows/*.json
    uv run python scripts/build_workflows.py --check    # fail if the outputs are stale
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.envfile import REPO_ROOT
from scripts.n8n_ids import CREDENTIALS, WORKFLOWS
from scripts.sql_text import placeholders_in_comments

SRC_DIR = REPO_ROOT / "n8n" / "src"
OUT_DIR = REPO_ROOT / "n8n" / "workflows"
INCLUDE_ROOTS = (REPO_ROOT / "sql", REPO_ROOT / "n8n" / "code")
PREFIX = "@@"


# Any JSON value. Templates are plain JSON, so this is exactly what resolve() walks.
type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None


class BuildError(Exception):
    """A template is invalid. The message names the template and the problem."""


def resolve(value: JsonValue, where: str) -> JsonValue:
    """Replace placeholders anywhere in a JSON value, recursively."""
    if isinstance(value, dict):
        return {key: resolve(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if not (isinstance(value, str) and value.startswith(PREFIX)):
        return value

    kind, _, arg = value[len(PREFIX) :].partition(":")
    if kind == "include":
        path = (REPO_ROOT / arg).resolve()
        if not any(path.is_relative_to(root) for root in INCLUDE_ROOTS):
            raise BuildError(f"{where}: include outside sql/ or n8n/code/: {arg}")
        if not path.is_file():
            raise BuildError(f"{where}: include not found: {arg}")
        text = path.read_text(encoding="utf-8")
        # n8n's Postgres node evaluates {{ }} anywhere in the query text, comments included, so
        # a stray pair turns a SQL file into a broken expression at run time.
        if path.suffix == ".sql" and "{{" in text:
            raise BuildError(f"{where}: {arg} contains '{{{{', which n8n would evaluate")
        # pg-promise fills $n inside comments too, so a multi-line value breaks the statement.
        if path.suffix == ".sql" and (bad := placeholders_in_comments(text)):
            raise BuildError(f"{where}: {arg} names a parameter as $n in a comment ({bad[0]!r}); "
                             "write #n in comments")  # fmt: skip
        return text
    if kind == "credential":
        if arg not in CREDENTIALS:
            raise BuildError(f"{where}: unknown credential {arg!r}")
        return {"id": CREDENTIALS[arg]["id"], "name": CREDENTIALS[arg]["name"]}
    if kind == "workflow":
        if arg not in WORKFLOWS:
            raise BuildError(f"{where}: unknown workflow {arg!r}")
        return WORKFLOWS[arg]
    raise BuildError(f"{where}: unknown placeholder {value!r}")


def validate(workflow: dict[str, Any], stem: str) -> None:
    """Catch the mistakes n8n would only reveal at import or run time."""
    for key in ("name", "nodes", "connections", "settings"):
        if key not in workflow:
            raise BuildError(f"{stem}: missing top-level {key!r}")
    names = [node["name"] for node in workflow["nodes"]]
    if len(names) != len(set(names)):
        raise BuildError(f"{stem}: duplicate node names")
    ids = [node.get("id") for node in workflow["nodes"]]
    if None in ids or len(ids) != len(set(ids)):
        raise BuildError(f"{stem}: every node needs a unique id")
    for source, outputs in workflow["connections"].items():
        if source not in names:
            raise BuildError(f"{stem}: connection from unknown node {source!r}")
        for branch in outputs.get("main", []):
            for link in branch or []:
                if link["node"] not in names:
                    raise BuildError(f"{stem}: connection to unknown node {link['node']!r}")


def build_one(template: Path) -> dict[str, Any]:
    """Build one workflow: resolve placeholders, pin its ID, validate."""
    stem = template.stem
    if stem not in WORKFLOWS:
        raise BuildError(f"{stem}: no ID registered in scripts/n8n_ids.py")
    resolved = resolve(json.loads(template.read_text(encoding="utf-8")), stem)
    if not isinstance(resolved, dict):
        raise BuildError(f"{stem}: a template must be a JSON object")
    workflow: dict[str, Any] = resolved
    workflow = {"id": WORKFLOWS[stem], **{k: v for k, v in workflow.items() if k != "id"}}
    workflow.setdefault("active", False)
    workflow.setdefault("pinData", {})
    validate(workflow, stem)
    return workflow


def render(workflow: dict[str, Any]) -> str:
    """Serialise deterministically, so a rebuild with no source changes is byte-identical."""
    return json.dumps(workflow, indent=2, ensure_ascii=False) + "\n"


def build_all(src_dir: Path = SRC_DIR) -> dict[str, str]:
    """Map output filename to rendered JSON for every template."""
    templates = sorted(src_dir.glob("*.json"))
    if not templates:
        raise BuildError(f"no templates in {src_dir}")
    return {f"{t.stem}.json": render(build_one(t)) for t in templates}


def main(argv: list[str] | None = None) -> int:
    """Write the outputs, or with --check report any that are missing or stale."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    try:
        outputs = build_all()
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale = [
        name
        for name, text in outputs.items()
        if not (OUT_DIR / name).exists() or (OUT_DIR / name).read_text(encoding="utf-8") != text
    ]
    extra = sorted(p.name for p in OUT_DIR.glob("*.json") if p.name not in outputs)
    if args.check:
        if stale or extra:
            print(f"stale: {stale}, unexpected: {extra}. Run `make build-workflows`.")
            return 1
        print(f"n8n/workflows is up to date ({len(outputs)} workflows)")
        return 0

    for name, text in outputs.items():
        (OUT_DIR / name).write_text(text, encoding="utf-8")
    for name in extra:
        (OUT_DIR / name).unlink()
    print(f"built {len(outputs)} workflows ({len(stale)} changed) into {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
