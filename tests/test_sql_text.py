"""The SQL comment scanner behind the build's "no $n in comments" rule (DECISIONS V35)."""

import pytest

from scripts.envfile import REPO_ROOT
from scripts.sql_text import comments, placeholders_in_comments

WORKFLOW_SQL = sorted(
    p for p in (REPO_ROOT / "sql").glob("*/*.sql") if p.parent.name != "migrations"
)


def test_comments_are_found_outside_strings_only() -> None:
    sql = (
        "-- head #1\n"
        "SELECT '-- not a comment', 'it''s', \"--id\", $$ -- body $$ /* a /* nested */ b */\n"
        "FROM t -- tail $2\n"
    )
    assert comments(sql) == ["-- head #1", "/* a /* nested */ b */", "-- tail $2"]
    assert placeholders_in_comments(sql) == ["-- tail $2"]


def test_a_cast_is_not_a_dollar_quote() -> None:
    assert comments("SELECT $1::uuid, $2 -- note\n") == ["-- note"]


@pytest.mark.parametrize("path", WORKFLOW_SQL, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_workflow_sql_names_a_parameter_in_a_comment(path: object) -> None:
    from pathlib import Path

    assert isinstance(path, Path)
    assert placeholders_in_comments(path.read_text(encoding="utf-8")) == []
