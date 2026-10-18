"""Generate synthetic users that match the sample CSV's distributions, or a wider profile.

Why: the bench measures Workflow B at 1k, 10k and 100k users, and the ingest tests need dirty
rows that the real file doesn't have. In the sample, every field is independent of the others
(|r| < 0.05, docs/PROFILE.md). So each field is drawn from the sample's own observed values,
and a generated file matches the real one wherever matching is concerned. A seed makes every
run reproducible.

Profiles:
    real  the sample's own marginals: income 25k-150k in 1k steps, score 650-800 in steps of 5,
          age 22-50, three employment types.
    wide  broader ranges (income 8k-400k, score 300-900, age 18-75), so the hard filter has
          real work to do. This is the bench's secondary profile.

Usage:
    uv run python scripts/gen_users.py --n 10000 --seed 42 --out data/generated/users-10k.csv
    uv run python scripts/gen_users.py --n 1000 --profile wide --out data/generated/wide-1k.csv
    uv run python scripts/gen_users.py --n 1000 --dirty-rate 0.05 --out data/generated/dirty.csv
"""

import argparse
import csv
import math
import random
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from scripts.envfile import REPO_ROOT

SAMPLE = REPO_ROOT / "data" / "users.csv"
HEADER = ("user_id", "name", "email", "monthly_income", "credit_score", "employment_status", "age")
EMPLOYMENT = ("Salaried", "Self-Employed", "Business")
Profile = Literal["real", "wide"]
Row = dict[str, str]


def sample_marginals(path: Path = SAMPLE) -> dict[str, list[str]]:
    """Observed values per column, repeats kept, so random.choice follows the sample's mix."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    columns = ("monthly_income", "credit_score", "employment_status", "age")
    values = {column: [row[column] for row in rows] for column in columns}
    values["first"] = [row["name"].split()[0] for row in rows]
    values["last"] = [row["name"].split()[-1] for row in rows]
    return values


def _wide_fields(rng: random.Random) -> tuple[str, str, str, str]:
    income = round(math.exp(rng.uniform(math.log(8_000), math.log(400_000))) / 1000) * 1000
    score = min(900, max(300, round(rng.gauss(700, 90) / 5) * 5))
    return str(income), str(score), rng.choice(EMPLOYMENT), str(rng.randint(18, 75))


def generate(n: int, seed: int = 42, profile: Profile = "real") -> list[Row]:
    """Return n clean users. The same arguments always give the same users."""
    rng = random.Random(seed)
    marginals = sample_marginals()
    rows: list[Row] = []
    for _ in range(n):
        first = rng.choice(marginals["first"])
        last = rng.choice(marginals["last"])
        if profile == "real":
            income = rng.choice(marginals["monthly_income"])
            score = rng.choice(marginals["credit_score"])
            employment = rng.choice(marginals["employment_status"])
            age = rng.choice(marginals["age"])
        else:
            income, score, employment, age = _wide_fields(rng)
        rows.append(
            {
                "user_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                "name": f"{first} {last}",
                "email": f"{first}.{last}@example.com".lower(),
                "monthly_income": income,
                "credit_score": score,
                "employment_status": employment,
                "age": age,
            }
        )
    return rows


# Each corruption breaks exactly one rule that the profile and the ingest validator enforce.
CORRUPTIONS: dict[str, Callable[[Row, list[Row]], Row]] = {
    "missing_value": lambda r, _: {**r, "monthly_income": ""},
    "non_integer": lambda r, _: {**r, "credit_score": "seven hundred"},
    "out_of_range": lambda r, _: {**r, "age": "150"},
    "unknown_employment": lambda r, _: {**r, "employment_status": "Student"},
    "bad_email": lambda r, _: {**r, "email": "not-an-email"},
    "padded_value": lambda r, _: {**r, "monthly_income": f" {r['monthly_income']} "},
    "duplicate_user_id": lambda r, prev: {**r, "user_id": prev[-1]["user_id"]} if prev else r,
}


def dirty(rows: list[Row], rate: float, seed: int) -> tuple[list[Row], dict[str, int]]:
    """Corrupt about rate * n rows, cycling through every corruption, and count each kind."""
    rng = random.Random(seed + 1)
    kinds = list(CORRUPTIONS)
    counts = dict.fromkeys(kinds, 0)
    out: list[Row] = []
    for row in rows:
        if out and rng.random() < rate:
            kind = kinds[sum(counts.values()) % len(kinds)]
            row = CORRUPTIONS[kind](row, out)
            counts[kind] += 1
        out.append(row)
    return out, counts


def write_csv(rows: list[Row], path: Path) -> None:
    """Write with CRLF line endings, like the sample file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile", choices=["real", "wide"], default="real")
    parser.add_argument("--dirty-rate", type=float, default=0.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = generate(args.n, args.seed, args.profile)
    counts: dict[str, int] = {}
    if args.dirty_rate > 0:
        rows, counts = dirty(rows, args.dirty_rate, args.seed)
    write_csv(rows, args.out)
    print(f"wrote {len(rows):,} users ({args.profile}, seed {args.seed}) to {args.out}")
    if counts:
        print(f"  dirty rows: {sum(counts.values())} {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
