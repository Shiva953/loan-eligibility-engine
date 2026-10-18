"""Run a workflow SQL file the way n8n's Postgres node runs it.

Why: n8n (pg-promise) formats the $1..$n parameters into the query text on the client and sends
the result as one simple-protocol message. A file may therefore hold several statements, and
they share one implicit transaction: sql/match/write_matches.sql starts with SET LOCAL for
exactly that reason. psycopg's default server-side binding rejects multi-statement text, so
tests and the bench go through a ClientCursor, which binds the same way n8n does.
"""

import re
from typing import Any

import psycopg

from scripts.envfile import REPO_ROOT


def load(name: str) -> str:
    """Read sql/<name> and convert its $n parameters to psycopg's %(pn)s style."""
    text = (REPO_ROOT / "sql" / name).read_text(encoding="utf-8").replace("%", "%%")
    return re.sub(r"\$(\d+)", r"%(p\1)s", text)


def params(*values: object) -> dict[str, object]:
    """Positional values as the named parameters `load` produces."""
    return {f"p{index}": value for index, value in enumerate(values, start=1)}


def run(conn: psycopg.Connection[Any], name: str, *values: object) -> list[tuple[Any, ...]]:
    """Execute every statement of sql/<name>; return the rows of the last one that has rows."""
    return [tuple(row.values()) for row in run_dicts(conn, name, *values)]


def run_dicts(conn: psycopg.Connection[Any], name: str, *values: object) -> list[dict[str, Any]]:
    """Like `run`, with each row keyed by column name."""
    rows: list[dict[str, Any]] = []
    with psycopg.ClientCursor(conn) as cur:
        cur.execute(load(name), params(*values))
        while True:
            if cur.description is not None:
                names = [column.name for column in cur.description]
                rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
            if not cur.nextset():
                break
    return rows
