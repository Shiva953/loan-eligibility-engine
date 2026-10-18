"""Brute-force reference for the deterministic stage: every (user, product) pair, in plain Python.

Why it exists: recall must be exactly 1.0, and "exactly" needs an independent ground truth.
This evaluator is deliberately naive. It loops over every pair, with no SQL, no bands and no
masks, and applies the hard rules exactly as stated: a NULL threshold never filters, ranges are
inclusive, and compilable filter rules (an employment-specific minimum income) apply only
when their condition holds. Every optimised variant must reproduce its output exactly.
"""

from dataclasses import dataclass
from typing import Any

import psycopg

FIELDS = ("monthly_income", "credit_score", "age")


@dataclass(frozen=True)
class Product:
    """The hard-filter view of one product."""

    product_id: int
    min_monthly_income: float | None
    min_credit_score: int | None
    min_age: int | None
    max_age: int | None
    allowed_employment: tuple[str, ...] | None
    filter_rules: tuple[tuple[str, str, float, tuple[str, ...]], ...]
    has_fuzzy: bool


def load_products(conn: psycopg.Connection) -> list[Product]:
    """Read the matchable products (active, with published criteria) and their filter rules."""
    rows = conn.execute(
        "SELECT product_id, min_monthly_income, min_credit_score, min_age, max_age, "
        "allowed_employment, soft_criteria, has_fuzzy_criteria FROM loan_products "
        "WHERE is_active AND has_criteria ORDER BY product_id"
    ).fetchall()
    products = []
    for pid, inc, cs, amin, amax, emp, soft, fuzzy in rows:
        rules = []
        for s in soft:
            rule = s.get("rule")
            if (
                s.get("class") != "compilable"
                or not rule
                or rule.get("effect", "filter") != "filter"
            ):
                continue
            when = tuple((rule.get("when") or {}).get("employment_status") or ())
            rules.append((rule["field"], rule["op"], float(rule["value"]), when))
        products.append(
            Product(
                pid,
                None if inc is None else float(inc),
                cs,
                amin,
                amax,
                None if emp is None else tuple(emp),
                tuple(rules),
                bool(fuzzy),
            )
        )
    return products


def passes(user: dict[str, Any], p: Product) -> bool:
    """Apply one product's hard rules to one user, exactly as written."""
    if p.min_monthly_income is not None and user["monthly_income"] < p.min_monthly_income:
        return False
    if p.min_credit_score is not None and user["credit_score"] < p.min_credit_score:
        return False
    if p.min_age is not None and user["age"] < p.min_age:
        return False
    if p.max_age is not None and user["age"] > p.max_age:
        return False
    if p.allowed_employment is not None and user["employment_status"] not in p.allowed_employment:
        return False
    for field, op, value, when in p.filter_rules:
        if when and user["employment_status"] not in when:
            continue
        actual = user[field]
        if (op == ">=" and actual < value) or (op == "<=" and actual > value):
            return False
    return True


def evaluate(conn: psycopg.Connection, batch_id: str) -> dict[tuple[str, int], str]:
    """Return {(user_id, product_id): 'matched' | 'pending_llm'} for every hard-rule pass."""
    products = load_products(conn)
    users = conn.execute(
        "SELECT user_id::text, monthly_income::float, credit_score, employment_status, age "
        "FROM users WHERE batch_id = %s",
        (batch_id,),
    ).fetchall()
    out: dict[tuple[str, int], str] = {}
    for uid, inc, cs, emp, age in users:
        user = {"monthly_income": inc, "credit_score": cs, "employment_status": emp, "age": age}
        for p in products:
            if passes(user, p):
                out[(uid, p.product_id)] = "pending_llm" if p.has_fuzzy else "matched"
    return out


@dataclass(frozen=True)
class Score:
    """How a variant's pair set compares with the reference."""

    reference: int
    produced: int
    true_positive: int

    @property
    def recall(self) -> float:
        """Share of reference pairs the variant produced (must be 1.0)."""
        return self.true_positive / self.reference if self.reference else 1.0

    @property
    def precision(self) -> float:
        """Share of produced pairs that the reference also has."""
        return self.true_positive / self.produced if self.produced else 1.0


def score(reference: dict[tuple[str, int], str], produced: set[tuple[str, int]]) -> Score:
    """Compare a produced pair set with the reference."""
    ref = set(reference)
    return Score(len(ref), len(produced), len(ref & produced))
