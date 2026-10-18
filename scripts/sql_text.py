"""Find the comments in a SQL file the way Postgres reads them.

Why: n8n's Postgres node formats the query with pg-promise, which replaces $1, $2, ... anywhere
in the text, comments included. A comment that names a parameter receives that parameter's
value; when the value has a line break (Gemini's 400 and 429 messages are multi-line), the
comment ends there and the rest of the value is parsed as SQL: "syntax error at or near '*'"
(docs/DECISIONS.md V35). So workflow SQL refers to its parameters as #1, #2 in comments, and
the build refuses a placeholder inside a comment.

The scanner skips string literals ('it''s'), quoted identifiers and dollar-quoted bodies, so a
`--` inside a string is not mistaken for a comment.
"""

import re

PLACEHOLDER = re.compile(r"\$\d")
DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _end_of_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the literal or identifier that opens at `start` (doubled quote escapes)."""
    i = start + 1
    while i < len(sql):
        if sql[i] == quote:
            if sql.startswith(quote * 2, i):
                i += 2
                continue
            return i + 1
        i += 1
    return len(sql)


def _end_of_block_comment(sql: str, start: int) -> int:
    """Index just past a /* ... */ comment; Postgres nests them."""
    depth, i = 1, start + 2
    while i < len(sql) and depth:
        if sql.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif sql.startswith("*/", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return i


def comments(sql: str) -> list[str]:
    """Every comment in `sql`, line (--) and block (/* */), in order."""
    found: list[str] = []
    i = 0
    while i < len(sql):
        if sql[i] in "'\"":
            i = _end_of_quoted(sql, i, sql[i])
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            found.append(sql[i:end])
            i = end
        elif sql.startswith("/*", i):
            end = _end_of_block_comment(sql, i)
            found.append(sql[i:end])
            i = end
        elif sql[i] == "$" and (tag := DOLLAR_TAG.match(sql, i)):
            end = sql.find(tag.group(0), tag.end())
            i = len(sql) if end < 0 else end + len(tag.group(0))
        else:
            i += 1
    return found


def placeholders_in_comments(sql: str) -> list[str]:
    """The comments that contain a $n placeholder, which pg-promise would fill in."""
    return [comment for comment in comments(sql) if PLACEHOLDER.search(comment)]
