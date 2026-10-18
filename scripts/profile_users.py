"""Profile a users CSV and write a deterministic Markdown report.

Why this exists: the ``users`` schema, the ingest validator and the cohort design in
``docs/PLAN.md`` all depend on facts about the sample data, for example that emails repeat
and that the fields are independent. This script measures those facts so they can be
reproduced, not just remembered. The report contains no timestamps, so
``make profile-check`` can compare it against the committed ``docs/PROFILE.md``.

Usage:
    uv run python scripts/profile_users.py data/users.csv --out docs/PROFILE.md
    uv run python scripts/profile_users.py data/users.csv --out docs/PROFILE.md --check
"""

import argparse
import codecs
import contextlib
import csv
import io
import math
import re
import statistics
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Self

EXPECTED_HEADER: tuple[str, ...] = (
    "user_id",
    "name",
    "email",
    "monthly_income",
    "credit_score",
    "employment_status",
    "age",
)
NUMERIC_COLUMNS: tuple[str, ...] = ("monthly_income", "credit_score", "age")
# The fields a lender criterion can depend on, and so the fields a cohort signature is built from.
MATCHING_COLUMNS: tuple[str, ...] = ("monthly_income", "credit_score", "employment_status", "age")
# These bounds catch garbage, not policy. Lender thresholds decide eligibility later, so the
# bounds are wide on purpose: a valid but unusual user must never be rejected at ingest.
PLAUSIBLE_RANGES: dict[str, tuple[int, int]] = {
    "monthly_income": (1, 10_000_000),
    "credit_score": (300, 900),
    "age": (18, 100),
}
KNOWN_EMPLOYMENT: frozenset[str] = frozenset({"Salaried", "Self-Employed", "Business"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RULES: tuple[str, ...] = (
    "wrong_field_count",
    "missing_value",
    "padded_value",
    "bad_user_id",
    "duplicate_user_id",
    "bad_email",
    "non_integer",
    "out_of_range",
    "unknown_employment",
)


@dataclass(frozen=True)
class NumericStats:
    """Summary of one integer column.

    ``step`` is the GCD of all the values. It shows how the data was rounded, for example
    incomes in whole thousands, and that rounding limits how fine a cohort band can usefully be.
    """

    minimum: int
    maximum: int
    mean: float
    median: float
    stdev: float
    distinct: int
    step: int

    @classmethod
    def of(cls, values: list[int]) -> Self:
        """Summarise a non-empty list of integers."""
        return cls(
            minimum=min(values),
            maximum=max(values),
            mean=statistics.fmean(values),
            median=float(statistics.median(values)),
            stdev=statistics.pstdev(values),
            distinct=len(set(values)),
            step=math.gcd(*values),
        )


@dataclass
class Profile:
    """Measured facts about one CSV, kept apart from rendering so tests can assert on numbers."""

    path: str
    size_bytes: int
    has_bom: bool
    line_endings: str
    header: list[str]
    rows: int = 0
    clean_rows: int = 0
    field_counts: Counter[int] = field(default_factory=Counter)
    violations: Counter[str] = field(default_factory=Counter)
    empty_cells: Counter[str] = field(default_factory=Counter)
    uuid_versions: Counter[int] = field(default_factory=Counter)
    distinct_emails: int = 0
    users_per_email: Counter[int] = field(default_factory=Counter)
    distinct_names: int = 0
    numeric: dict[str, NumericStats] = field(default_factory=dict)
    employment: Counter[str] = field(default_factory=Counter)
    cohort_cardinality: dict[tuple[str, ...], int] = field(default_factory=dict)
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)

    @property
    def dirty_rows(self) -> int:
        """Count the rows that broke at least one rule."""
        return self.rows - self.clean_rows

    @property
    def max_users_per_email(self) -> int:
        """Return the largest number of users that share one email address."""
        return max(self.users_per_email, default=0)


def detect_line_endings(text: str) -> str:
    """Name the line-ending style. The ingest reader must handle CRLF, which this file uses."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def row_violations(record: dict[str, str], seen_ids: set[str]) -> list[str]:
    """List the rules a row breaks, and record its user_id so later duplicates are caught.

    These are the same rules the ingest Lambda will enforce, so the profile's "dirty rows"
    count predicts what ingest would reject.
    """
    problems: list[str] = []
    if any(value == "" for value in record.values()):
        problems.append("missing_value")
    if any(value != value.strip() for value in record.values()):
        problems.append("padded_value")

    user_id = record["user_id"]
    try:
        uuid.UUID(user_id)
    except ValueError:
        problems.append("bad_user_id")
    else:
        if user_id in seen_ids:
            problems.append("duplicate_user_id")
        seen_ids.add(user_id)

    if not EMAIL_RE.fullmatch(record["email"]):
        problems.append("bad_email")
    for column in NUMERIC_COLUMNS:
        try:
            value = int(record[column])
        except ValueError:
            problems.append("non_integer")
            continue
        low, high = PLAUSIBLE_RANGES[column]
        if not low <= value <= high:
            problems.append("out_of_range")
    if record["employment_status"] not in KNOWN_EMPLOYMENT:
        problems.append("unknown_employment")
    return sorted(set(problems))


def profile_csv(path: Path) -> Profile:
    """Read the whole CSV and measure it.

    The profiler reads everything into memory because it runs on a developer machine against
    files of at most about 10 MB. The ingest Lambda streams instead.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, [])
    profile = Profile(
        path=path.as_posix(),
        size_bytes=len(raw),
        has_bom=raw.startswith(codecs.BOM_UTF8),
        line_endings=detect_line_endings(text),
        header=header,
    )

    seen_ids: set[str] = set()
    clean: list[dict[str, str]] = []
    for row in reader:
        profile.rows += 1
        profile.field_counts[len(row)] += 1
        if len(row) != len(header):
            profile.violations["wrong_field_count"] += 1
            continue
        record = dict(zip(header, row, strict=True))
        for column, value in record.items():
            if value.strip() == "":
                profile.empty_cells[column] += 1
        problems = row_violations(record, seen_ids)
        profile.violations.update(problems)
        if not problems:
            clean.append(record)

    profile.clean_rows = len(clean)
    if not clean:
        return profile

    profile.uuid_versions = Counter(uuid.UUID(r["user_id"]).version or 0 for r in clean)
    emails = Counter(r["email"].lower() for r in clean)
    profile.distinct_emails = len(emails)
    profile.users_per_email = Counter(emails.values())
    profile.distinct_names = len({r["name"] for r in clean})
    numbers = {c: [int(r[c]) for r in clean] for c in NUMERIC_COLUMNS}
    profile.numeric = {c: NumericStats.of(v) for c, v in numbers.items()}
    profile.employment = Counter(r["employment_status"] for r in clean)
    for size in range(1, len(MATCHING_COLUMNS) + 1):
        for subset in combinations(MATCHING_COLUMNS, size):
            profile.cohort_cardinality[subset] = len({tuple(r[c] for c in subset) for r in clean})
    for a, b in combinations(NUMERIC_COLUMNS, 2):
        # Fewer than two rows or a constant column leaves correlation undefined; omit the pair.
        with contextlib.suppress(statistics.StatisticsError):
            profile.correlations[(a, b)] = statistics.correlation(numbers[a], numbers[b])
    return profile


def render_markdown(p: Profile) -> str:
    """Render the profile as Markdown, with the same output for the same input."""
    header_ok = "yes" if tuple(p.header) == EXPECTED_HEADER else "no"
    out: list[str] = [
        "# Users CSV profile",
        "",
        f"Generated by `scripts/profile_users.py` from `{p.path}`. Don't edit this file by hand:",
        "run `make profile` to regenerate it.",
        "",
        "## File",
        "",
        "| Property | Value |",
        "|---|---|",
        f"| Size | {p.size_bytes:,} bytes |",
        f"| Encoding | UTF-8, BOM: {'yes' if p.has_bom else 'no'} |",
        f"| Line endings | {p.line_endings} |",
        f"| Header | `{', '.join(p.header)}` |",
        f"| Header matches the expected columns | {header_ok} |",
        f"| Data rows | {p.rows:,} |",
        "| Fields per row | "
        + ", ".join(f"{n} fields: {c:,} rows" for n, c in sorted(p.field_counts.items()))
        + " |",
        "",
        "## Validity",
        "",
        "The ingest validator will enforce the same rules (see `row_violations`). A row can",
        "break more than one rule.",
        "",
        "| Rule | Rows breaking it |",
        "|---|---|",
        *(f"| `{rule}` | {p.violations[rule]:,} |" for rule in RULES),
        "",
        f"**Dirty rows: {p.dirty_rows:,} of {p.rows:,}.** Clean rows: {p.clean_rows:,}.",
        "Every section below uses clean rows only.",
        "",
    ]
    if not p.clean_rows:
        return "\n".join(out)

    versions = ", ".join(f"v{v}: {c:,}" for v, c in sorted(p.uuid_versions.items()))
    out += [
        "## Identity columns",
        "",
        "| Column | Distinct | Notes |",
        "|---|---|---|",
        f"| `user_id` | {p.clean_rows:,} | UUID versions: {versions} |",
        f"| `name` | {p.distinct_names:,} | not in the brief's list; kept for the greeting |",
        f"| `email` | {p.distinct_emails:,} | up to {p.max_users_per_email} users share one |",
        "",
        "### Users per email",
        "",
        "| Users sharing the email | Emails |",
        "|---|---|",
        *(f"| {k} | {v:,} |" for k, v in sorted(p.users_per_email.items())),
        "",
        "## Numeric columns",
        "",
        "| Column | Min | Max | Step (GCD) | Distinct | Mean | Median | Std dev |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| `{c}` | {s.minimum:,} | {s.maximum:,} | {s.step:,} | {s.distinct:,} "
            f"| {s.mean:,.1f} | {s.median:,.1f} | {s.stdev:,.1f} |"
            for c, s in p.numeric.items()
        ),
        "",
        "## `employment_status`",
        "",
        "| Value | Rows | Share |",
        "|---|---|---|",
        *(
            f"| {v} | {c:,} | {c / p.clean_rows:.1%} |"
            for v, c in sorted(p.employment.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "",
        "## Cohort cardinality",
        "",
        "Distinct signatures if a cohort is keyed on these fields' raw values, before any",
        "banding. A fuzzy criterion's `depends_on` decides which row applies.",
        "",
        "| Fields in the signature | Distinct signatures |",
        "|---|---|",
        *(
            f"| {', '.join(f'`{c}`' for c in k)} | {v:,} |"
            for k, v in sorted(p.cohort_cardinality.items(), key=lambda kv: (len(kv[0]), kv[1]))
        ),
        "",
        "## Pairwise correlation (Pearson r)",
        "",
        "| Pair | r |",
        "|---|---|",
        *(f"| `{a}` vs `{b}` | {r:+.3f} |" for (a, b), r in p.correlations.items()),
        "",
        "## What this means for the design",
        "",
        *implications(p),
        "",
    ]
    return "\n".join(out)


def implications(p: Profile) -> list[str]:
    """Derive the design consequences from the numbers, so they stay true if the data changes."""
    notes: list[str] = []
    if p.distinct_emails < p.clean_rows:
        notes.append(
            f"- `email` is not unique ({p.distinct_emails:,} distinct across {p.clean_rows:,} "
            "users), so `users.email` has no unique constraint and notifications are keyed on "
            "`user_id`."
        )
    full = p.cohort_cardinality[MATCHING_COLUMNS]
    employment_only = p.cohort_cardinality[("employment_status",)]
    notes.append(
        f"- Cohorts keyed on the full profile would save almost nothing ({full:,} signatures for "
        f"{p.clean_rows:,} users). Cohorts keyed on one criterion's `depends_on` collapse to a "
        f"handful: an employment-only criterion has {employment_only} signatures per product."
    )
    if p.dirty_rows == 0:
        notes.append(
            "- The file has no dirty rows, so `gen_users.py --dirty-rate` injects bad rows to "
            "exercise the ingest reject path."
        )
    if p.line_endings != "LF":
        notes.append(f"- Line endings are {p.line_endings}: the ingest reader must not assume LF.")
    if p.correlations and max(abs(r) for r in p.correlations.values()) < 0.05:
        notes.append(
            "- The numeric fields are independent (|r| < 0.05), so the generator can sample each "
            "field from its own marginal distribution and still match the sample."
        )
    for column, stats in p.numeric.items():
        if stats.step > 1:
            notes.append(
                f"- `{column}` comes in steps of {stats.step:,}, so a cohort band finer than "
                "that can't separate any users."
            )
    return notes


def main(argv: list[str] | None = None) -> int:
    """Run the CLI: write the report, or with ``--check`` fail if the committed copy is stale."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("csv", type=Path, help="users CSV to profile")
    parser.add_argument("--out", type=Path, help="write Markdown here instead of stdout")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if --out differs from a fresh profile"
    )
    args = parser.parse_args(argv)

    report = render_markdown(profile_csv(args.csv))
    if args.check:
        if args.out is None:
            parser.error("--check needs --out")
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != report:
            print(f"{args.out} is stale: run `make profile`", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    if args.out is None:
        sys.stdout.write(report)
    else:
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
