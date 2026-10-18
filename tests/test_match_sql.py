"""Workflow B's SQL against a real Postgres, run as n8n_app the way n8n runs it.

Each statement goes through scripts.n8n_sql (client-side binding, the whole file as one
message), so multi-statement files and the role's grants are exercised exactly as in n8n.
Products are the synthetic bench set, which covers every criterion class.
"""

import json
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from bench import benchdb, reference
from scripts import migrate, seed_config
from scripts.dbconn import admin_connect_kwargs
from scripts.gen_users import Profile, generate
from scripts.n8n_sql import run, run_dicts

Conns = tuple[psycopg.Connection, psycopg.Connection]


@pytest.fixture
def env(scratch_db: str, local_settings: dict[str, str]) -> Iterator[Conns]:
    """(admin, n8n_app) connections to a migrated scratch database with the synthetic products."""
    migrate.run("local", scratch_db)
    seed_config.store("local", seed_config.build_config(local_settings), scratch_db)
    kwargs = admin_connect_kwargs(local_settings, "local", scratch_db)
    admin = psycopg.connect(**kwargs, autocommit=True)
    benchdb.load_synthetic_products(admin)
    app_kwargs = {**kwargs, "user": "n8n_app", "password": local_settings["N8N_APP_DB_PASSWORD"]}
    app = psycopg.connect(**app_kwargs, autocommit=True)
    try:
        yield admin, app
    finally:
        app.close()
        admin.close()


def batch(admin: psycopg.Connection, n: int, seed: int, profile: Profile = "real") -> str:
    return benchdb.load_batch(admin, generate(n, seed, profile), f"test-{seed}")


def write(
    app: psycopg.Connection,
    batch_id: str,
    mode: str = "coarse",
    llm: str = "true",
    rules: str = "true",
) -> dict[str, Any]:
    return run_dicts(app, "match/write_matches.sql", batch_id, mode, llm, "test", rules)[0]


def batch_pairs(conn: psycopg.Connection, batch_id: str) -> dict[tuple[str, int], tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT user_id::text, product_id, status, decided_by, caveats, reason FROM matches "
        "WHERE batch_id = %s",
        (batch_id,),
    ).fetchall()
    return {(u, p): tuple(rest) for u, p, *rest in rows}


def store(
    app: psycopg.Connection,
    batch_id: str | None,
    decisions: list[dict[str, Any]],
    rules: list[dict[str, Any]] | None = None,
) -> None:
    stats = {
        "calls": 1,
        "tokens_in": 10,
        "tokens_out": 5,
        "requests": 1,
        "failed_requests": 0,
        "unanswered": 0,
    }
    run(
        app,
        "match/store_decisions.sql",
        json.dumps(decisions),
        batch_id,
        "test",
        json.dumps(stats),
        json.dumps(rules or []),
    )


def decision(cohort: dict[str, Any], verdict: str) -> dict[str, Any]:
    return {
        "criterion_key": cohort["criterion_key"],
        "signature": cohort["signature"],
        "verdict": verdict,
        "reason": f"test {verdict}",
        "model": "test",
        "prompt_version": "t",
    }


def find_uncached(
    app: psycopg.Connection,
    batch_id: str,
    mode: str = "coarse",
    llm: str = "true",
    rules: str = "true",
    claim: str = "false",
) -> dict[str, Any]:
    return run_dicts(app, "match/uncached_cohorts.sql", batch_id, mode, llm, "test", rules, claim)[
        0
    ]


def uncached(app: psycopg.Connection, batch_id: str, mode: str = "coarse") -> list[dict[str, Any]]:
    cohorts: list[dict[str, Any]] = find_uncached(app, batch_id, mode)["cohorts"]
    return cohorts


# --- S2: the hard filter -----------------------------------------------------------------------


def test_hard_filter_equals_the_brute_force_reference(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 500, 11, "wide")
    rows = app.execute(
        "SELECT user_id::text, product_id, has_fuzzy FROM match_candidates(%s)", (batch_id,)
    )
    got = {(u, p): "pending_llm" if fuzzy else "matched" for u, p, fuzzy in rows.fetchall()}
    assert got == reference.evaluate(admin, batch_id)
    assert 0 < len(got) < 500 * 25  # the wide profile fails some rules and passes others


# --- S3: cohort signatures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "lo", "hi", "mode", "expected"),
    [
        ("monthly_income", 37000, 30000, None, "threshold", "30000.."),
        ("monthly_income", 37000, 30000, None, "coarse", "30000..49999"),
        ("monthly_income", 37000, 30000, None, "fine", "30000..39999"),
        ("monthly_income", 37000, 30000, None, "exact", "37000"),
        ("monthly_income", 250000, 30000, None, "fine", "200000.."),
        ("monthly_income", 120000, None, None, "coarse", "100000.."),
        ("credit_score", 640, 600, None, "coarse", "600..649"),
        ("age", 58, 21, 60, "coarse", "45..60"),
        ("age", 23, 21, 60, "fine", "21..24"),
    ],
)
def test_band_label(
    env: Conns, field: str, value: int, lo: int | None, hi: int | None, mode: str, expected: str
) -> None:
    _, app = env
    row = app.execute(
        "SELECT band_label(%s, %s, %s, %s, %s)", (field, value, lo, hi, mode)
    ).fetchone()
    assert row == (expected,)


def test_cohorts_are_shared_across_products_and_name_only_their_dependencies(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 400, 12)
    rows = app.execute(
        "SELECT product_id, criterion_key, criterion_text, depends_on, signature "
        "FROM fuzzy_signatures(%s, 'coarse')",
        (batch_id,),
    ).fetchall()
    keys, products = defaultdict(set), defaultdict(set)
    for product_id, key, text, depends_on, signature in rows:
        keys[text].add(key)
        products[text].add(product_id)
        assert [part.split("=")[0] for part in signature.split(";") if part] == sorted(depends_on)
    assert all(len(k) == 1 for k in keys.values())
    assert any(len(p) > 1 for p in products.values()), "the bench set repeats some criteria"


# --- S4 + S5: the cache and the write -----------------------------------------------------------


def test_cached_verdicts_serve_later_batches(env: Conns) -> None:
    admin, app = env
    first = batch(admin, 300, 13)
    cohorts = uncached(app, first)
    assert cohorts
    store(app, first, [decision(c, "eligible") for c in cohorts])
    assert uncached(app, first) == []
    # The same profile distribution reuses most cohorts: only unseen ones are asked again.
    second = batch(admin, 300, 14)
    assert len(uncached(app, second)) < len(cohorts)


def test_each_pair_takes_the_status_of_its_cohort_verdicts(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 400, 15)
    cohorts = find_uncached(app, batch_id, claim="true")["cohorts"]
    cycle = ["eligible", "ineligible", "uncertain", None]  # None: left undecided
    verdict = {(c["criterion_key"], c["signature"]): cycle[i % 4] for i, c in enumerate(cohorts)}
    store(
        app, batch_id, [decision(c, v) for c, v in zip(cohorts, verdict.values(), strict=True) if v]
    )

    summary = write(app, batch_id)
    got = batch_pairs(app, batch_id)
    assert set(got) == set(reference.evaluate(admin, batch_id))  # recall and precision 1.0
    assert summary["pairs"] == summary["rows_written"] == len(got)

    pair_verdicts: dict[tuple[str, int], list[str | None]] = defaultdict(list)
    for u, p, key, sig in app.execute(
        "SELECT user_id::text, product_id, criterion_key, signature "
        "FROM fuzzy_signatures(%s, 'coarse')",
        (batch_id,),
    ).fetchall():
        pair_verdicts[(u, p)].append(verdict[(key, sig)])
    for pair, (status, decided_by, caveats, reason) in got.items():
        verdicts = pair_verdicts.get(pair)
        if verdicts is None:
            assert (status, decided_by, caveats) == ("matched", "rule", [])
        elif None in verdicts:
            assert (status, decided_by, reason) == ("pending_llm", None, None)
        elif "ineligible" in verdicts:
            assert (status, decided_by) == ("llm_rejected", "llm")
        else:
            assert (status, decided_by) == ("llm_matched", "llm")
            assert ("uncertain" in verdicts) == any(
                c.startswith("May not apply: ") for c in caveats
            )


def test_rules_only_mode_matches_every_pair_and_marks_fuzzy_criteria_unchecked(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 200, 16)
    assert find_uncached(app, batch_id, llm="false")["cohorts_uncached"] == 0
    summary = write(app, batch_id, llm="false")
    got = batch_pairs(app, batch_id)
    assert summary["matched"] == summary["rows_written"] == len(got)
    fuzzy = {p for (_, p), s in reference.evaluate(admin, batch_id).items() if s == "pending_llm"}
    for (_, product_id), (_, _, caveats, _) in got.items():
        assert any(c.startswith("Not checked: ") for c in caveats) == (product_id in fuzzy)


def test_reruns_and_reuploaded_users_take_the_upsert_path(env: Conns) -> None:
    admin, app = env
    first = batch(admin, 200, 17)
    assert write(app, first)["returning_users"] == 0
    before = batch_pairs(app, first)

    # A replay of the same batch goes through the upsert path and rewrites nothing.
    again = write(app, first)
    assert again["returning_users"] > 0
    assert again["pairs"] == len(before)
    assert again["rows_written"] == 0
    assert batch_pairs(app, first) == before

    # A new upload moves half of the users (with doubled income) into a second batch next to
    # 50 new users: one statement writes both groups.
    second = batch(admin, 50, 18)
    moved = [
        u
        for (u,) in admin.execute(
            "SELECT user_id FROM users WHERE batch_id = %s ORDER BY user_id LIMIT 100", (first,)
        )
    ]
    admin.execute(
        "UPDATE users SET batch_id = %s, monthly_income = monthly_income * 2 "
        "WHERE user_id = ANY(%s)",
        (second, moved),
    )
    summary = write(app, second)
    assert 0 < summary["returning_users"] <= 100
    assert set(batch_pairs(app, second)) == set(reference.evaluate(admin, second))


def test_new_verdicts_rewrite_only_the_users_they_change_and_announce_them_again(
    env: Conns,
) -> None:
    admin, app = env
    batch_id = batch(admin, 200, 19)
    first = write(app, batch_id)  # nothing cached yet: every fuzzy pair is pending
    assert first["pending_llm"] > 0
    assert first["users_written"] > 0
    admin.execute(
        "UPDATE user_matches SET to_announce = false, notified_at = now() WHERE batch_id = %s",
        (batch_id,),
    )
    store(app, batch_id, [decision(c, "eligible") for c in uncached(app, batch_id)])
    summary = write(app, batch_id)
    assert summary["pending_llm"] == 0
    assert summary["llm_matched"] == first["pending_llm"]
    decided_users = admin.execute(
        "SELECT count(DISTINCT user_id) FROM matches "
        "WHERE batch_id = %s AND status = 'llm_matched'",
        (batch_id,),
    ).fetchone()
    assert decided_users
    assert summary["users_written"] == decided_users[0]
    # Users with newly decided pairs are news again; the others were announced and stay so.
    rows: dict[bool, int] = dict(
        app.execute(
            "SELECT to_announce, count(*) FROM user_matches WHERE batch_id = %s GROUP BY 1",
            (batch_id,),
        ).fetchall()
    )
    assert rows[True] == decided_users[0]
    assert rows.get(False, 0) == first["users_written"] - decided_users[0]


def clause(verdict: str, **bounds: float) -> dict[str, Any]:
    fields = [f"{f}_{e}" for f in ("age", "credit_score", "monthly_income") for e in ("min", "max")]
    return {"verdict": verdict, "employment_status": [], **dict.fromkeys(fields), **bounds}


def rule(criterion: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **criterion,
        "clauses": clauses,
        "note": "test rule",
        "model": "test",
        "prompt_version": "t",
    }


def test_criteria_without_a_rule_are_reported_until_compiled(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 300, 29)
    found = find_uncached(app, batch_id)
    assert found["criteria_uncompiled"] == len(found["criteria"]) > 0
    assert find_uncached(app, batch_id, rules="false")["criteria"] == []
    store(app, batch_id, [], [rule(c, []) for c in found["criteria"]])
    assert find_uncached(app, batch_id)["criteria"] == []


def test_the_cohort_and_rule_judges_vote(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 400, 30)
    found = find_uncached(app, batch_id)
    criteria = {c["criterion_key"]: c for c in found["criteria"]}
    # c1: the cohort judge is unsure and the rule rejects low incomes: the rule's veto decides.
    # c2: the cohort judge says eligible and the rule rejects low incomes: a conflict, uncertain.
    # c3: the cohort judge is unsure and the rule vouches for low incomes: still uncertain.
    # Every other criterion: the cohort judge is unsure and the rule has no clauses.
    c1, c2, c3 = sorted(criteria)[:3]
    cohort_votes = {c1: "uncertain", c2: "eligible", c3: "uncertain"}
    rejects_low = clause("ineligible", monthly_income_max=49999)
    vouches_low = clause("eligible", monthly_income_max=49999)
    clauses = {c1: [rejects_low], c2: [rejects_low], c3: [vouches_low]}
    store(
        app,
        batch_id,
        [decision(c, cohort_votes.get(c["criterion_key"], "uncertain")) for c in found["cohorts"]],
        [rule(c, clauses.get(k, [])) for k, c in criteria.items()],
    )
    write(app, batch_id)

    expected: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
    for u, p, key, income in app.execute(
        "SELECT s.user_id::text, s.product_id, s.criterion_key, u.monthly_income "
        "FROM fuzzy_signatures(%s, 'coarse') s JOIN users u USING (user_id)",
        (batch_id,),
    ).fetchall():
        low = income <= 49999
        expected[(u, p)][key] = (
            ("ineligible" if low else "uncertain")
            if key == c1
            else ("uncertain" if low else "eligible")
            if key == c2
            else "uncertain"
        )
    got = batch_pairs(app, batch_id)
    outcomes = set()
    for pair, votes in expected.items():
        status, decided_by, caveats, reason = got[pair]
        rejected = "ineligible" in votes.values()
        assert (status, decided_by) == ("llm_rejected" if rejected else "llm_matched", "llm")
        uncertain = {criteria[k]["criterion_text"] for k, v in votes.items() if v == "uncertain"}
        assert {c.removeprefix("May not apply: ") for c in caveats} == uncertain
        if votes.get(c1) == "ineligible":
            assert "ineligible (compiled rule)" in reason
        if c2 in votes and votes[c2] == "uncertain":
            assert "uncertain (the two judges disagree)" in reason
        outcomes.add((votes.get(c1), votes.get(c2)))
    assert ("ineligible", None) in outcomes or (
        "ineligible",
        "uncertain",
    ) in outcomes  # c1 rejects someone

    # With the rule judge off, the same cached verdicts reject nobody.
    off = write(app, batch_id, rules="false")
    assert off["llm_rejected"] == 0
    assert off["rows_written"] > 0


# --- Warming: judging the cohort space before any batch arrives ---------------------------------


@pytest.mark.parametrize("mode", ["threshold", "coarse", "fine"])
def test_the_cohort_space_covers_every_signature_a_batch_can_produce(env: Conns, mode: str) -> None:
    admin, app = env
    space = set(
        app.execute("SELECT criterion_key, signature FROM cohort_space(%s)", (mode,)).fetchall()
    )
    cases: tuple[tuple[int, Profile], ...] = ((31, "real"), (32, "wide"))
    for seed, profile in cases:
        batch_id = batch(admin, 2000, seed, profile)
        produced = set(
            app.execute(
                "SELECT DISTINCT criterion_key, signature FROM fuzzy_signatures(%s, %s)",
                (batch_id, mode),
            ).fetchall()
        )
        assert produced
        assert produced <= space, sorted(produced - space)[:5]
        if mode == "threshold":
            # Threshold bands don't depend on the values: the space is what batches produce.
            assert produced == space


def test_exact_banding_has_no_finite_space(env: Conns) -> None:
    _, app = env
    assert app.execute("SELECT count(*) FROM cohort_space('exact')").fetchone() == (0,)


def warm(app: psycopg.Connection, mode: str = "threshold", llm: str = "true") -> dict[str, Any]:
    return run_dicts(app, "match/warm_cohorts.sql", mode, llm, "true", "test")[0]


def test_after_warming_a_new_batch_has_nothing_left_to_ask(env: Conns) -> None:
    admin, app = env
    found = warm(app)
    assert found["cohorts_uncached"] == found["cohorts_total"] > 0
    assert found["criteria_uncompiled"] == found["criteria_total"] > 0
    store(
        app,
        None,
        [decision(c, "uncertain") for c in found["cohorts"]],
        [rule(c, []) for c in found["criteria"]],
    )
    again = warm(app)
    assert (again["cohorts_uncached"], again["criteria_uncompiled"]) == (0, 0)
    # The batch-side query now finds every cohort and criterion cached: no LLM call at match time.
    batch_id = batch(admin, 2000, 33, "wide")
    at_match_time = find_uncached(app, batch_id, mode="threshold")
    assert at_match_time["cohorts_total"] > 0
    assert (at_match_time["cohorts_uncached"], at_match_time["criteria_uncompiled"]) == (0, 0)


def test_warming_does_nothing_when_the_match_time_llm_is_off(env: Conns) -> None:
    _, app = env
    found = warm(app, llm="false")
    assert (found["cohorts_total"], found["cohorts"], found["criteria"]) == (0, [], [])


# --- Batch lifecycle -----------------------------------------------------------------------------


def test_the_match_model_latency_is_fitted_from_its_own_call_log(env: Conns) -> None:
    admin, app = env
    model = admin.execute("SELECT value FROM app_config WHERE key = 'llm_model_match'").fetchone()
    assert model
    for tokens in range(100, 3100, 100):  # 30 answered calls: 1 s fixed plus 2 ms per token
        admin.execute(
            "INSERT INTO llm_calls (execution_id, purpose, provider, model, attempt, outcome, "
            "tokens_in, tokens_out, duration_ms) VALUES ('t', 'judge_cohorts', 'gemini', %s, 1, "
            "'ok', 10, %s, %s)",
            (model[0], tokens, 1000 + 2 * tokens),
        )
    latency = find_uncached(app, batch(admin, 5, 26), "", "", "")["llm_latency"]
    assert (latency["calls"], latency["l0_ms"], latency["ms_per_token"], latency["r2"]) == (
        30,
        1000,
        2,
        1,
    )
    warm = run_dicts(app, "match/warm_cohorts.sql", "", "", "", "t")[0]
    assert warm["llm_latency"]["calls"] == 30


def test_a_batch_is_claimed_once_and_a_lost_claim_does_nothing(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 60, 20)
    first = find_uncached(app, batch_id, "", "", "", claim="true")
    assert (first["claimed"], first["status_before"]) == (True, "ingested")
    # Empty settings are read from app_config, and come back for Write matches to reuse.
    config = first["config"]
    assert (first["cohort_bands"], first["match_llm_enabled"], first["match_rules_enabled"]) == (
        config["cohort_bands"],
        config["match_llm_enabled"],
        config["match_rules_enabled"],
    )
    assert first["metrics_written"] == 2
    again = find_uncached(app, batch_id, claim="true")
    assert (again["claimed"], again["status_before"]) == (False, "matching")
    assert (again["cohorts"], again["criteria"], again["metrics_written"]) == ([], [], 0)


def test_writing_finishes_a_claimed_batch_and_fail_records_the_error(env: Conns) -> None:
    admin, app = env
    unclaimed = batch(admin, 50, 21)
    assert write(app, unclaimed)["finished"] is False  # the bench and tests write unclaimed
    status = "SELECT status FROM ingestion_batches WHERE batch_id = %s"
    assert admin.execute(status, (unclaimed,)).fetchone() == ("ingested",)

    batch_id = batch(admin, 50, 25)
    find_uncached(app, batch_id, claim="true")
    summary = write(app, batch_id)
    assert (summary["batch_id"], summary["finished"]) == (batch_id, True)
    assert summary["matching_ms"] >= 0
    assert admin.execute(status, (batch_id,)).fetchone() == ("matched",)
    stages = admin.execute(
        "SELECT array_agg(stage ORDER BY stage) FROM pipeline_metrics WHERE batch_id = %s",
        (batch_id,),
    ).fetchone()
    assert stages == (["s2_hard_filter", "s3_cohorts", "s4_cache", "write"],)

    other = batch(admin, 5, 22)
    assert run(app, "match/fail_batch.sql", other, "boom")[0] == (other, True)
    row = admin.execute("SELECT status, error FROM ingestion_batches WHERE batch_id = %s", (other,))
    assert row.fetchone() == ("failed", "boom")


def test_the_sweeper_finds_batches_whose_webhook_never_came(env: Conns) -> None:
    admin, app = env
    fresh, stale = batch(admin, 5, 23), batch(admin, 5, 24)
    admin.execute(
        "UPDATE ingestion_batches SET ingested_at = now() - interval '1 hour' WHERE batch_id = %s",
        (stale,),
    )
    found = [b for b, _ in run(app, "match/stalled_batches.sql", "", "sweeper")]
    assert stale in found
    assert fresh not in found
    # "Run manually" takes every waiting batch, however fresh, oldest first.
    manual = run(app, "match/stalled_batches.sql", "0", "manual")
    assert {fresh, stale} <= {b for b, _ in manual}
    assert {who for _, who in manual} == {"manual"}


# --- Referential integrity without row-level foreign keys ----------------------------------------


def test_matches_reject_references_to_missing_rows(env: Conns) -> None:
    admin, _ = env
    batch_id = batch(admin, 5, 25)
    (user_id,) = admin.execute("SELECT user_id FROM users LIMIT 1").fetchone() or (None,)
    (product_id,) = admin.execute("SELECT min(product_id) FROM loan_products").fetchone() or (None,)
    insert = (
        "INSERT INTO user_matches (user_id, product_ids, batch_id, statuses) "
        "VALUES (%s, ARRAY[%s]::bigint[], %s, '{matched}')"
    )
    missing = "00000000-0000-4000-8000-000000000000"
    for values in (
        (missing, product_id, batch_id),
        (user_id, -1, batch_id),
        (user_id, product_id, missing),
    ):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute(insert, values)
    admin.execute(insert, (user_id, product_id, batch_id))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute("UPDATE user_matches SET product_ids = '{-1}'")


def test_deleting_users_cascades_while_referenced_products_and_batches_stay(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 30, 26)
    write(app, batch_id)
    user_id, product_id = admin.execute(
        "SELECT user_id, product_id FROM matches LIMIT 1"
    ).fetchone() or (None, None)
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="referenced by matches"):
        admin.execute("DELETE FROM loan_products WHERE product_id = %s", (product_id,))
    # Leave the batch referenced by matches alone: its users moved on (a re-upload) and its
    # metrics are gone, so no other foreign key stands in the way.
    later = batch(admin, 1, 27)
    admin.execute("UPDATE users SET batch_id = %s WHERE batch_id = %s", (later, batch_id))
    admin.execute("DELETE FROM pipeline_metrics WHERE batch_id = %s", (batch_id,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="referenced by matches"):
        admin.execute("DELETE FROM ingestion_batches WHERE batch_id = %s", (batch_id,))
    admin.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    assert admin.execute(
        "SELECT count(*) FROM matches WHERE user_id = %s", (user_id,)
    ).fetchone() == (0,)


def test_the_write_counts_the_users_who_have_an_offer(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 300, 23)
    summary = write(app, batch_id)
    stored = admin.execute(
        "SELECT count(*) FROM user_matches WHERE batch_id = %s "
        "AND statuses && ARRAY['matched', 'llm_matched']",
        (batch_id,),
    ).fetchone()
    assert stored is not None
    assert summary["users_with_offers"] == stored[0] > 0


def test_a_product_without_published_criteria_matches_nobody(env: Conns) -> None:
    admin, app = env
    batch_id = batch(admin, 50, 29)
    admin.execute(
        "UPDATE loan_products SET min_monthly_income = NULL, min_credit_score = NULL, "
        "min_age = NULL, max_age = NULL, allowed_employment = NULL, soft_criteria = '[]' "
        "WHERE product_id = (SELECT min(product_id) FROM loan_products WHERE is_active)"
    )
    bare = [r[0] for r in admin.execute(
        "SELECT product_id FROM loan_products WHERE is_active AND NOT has_criteria"
    ).fetchall()]  # fmt: skip
    assert bare
    write(app, batch_id)
    offered = admin.execute(
        "SELECT count(*) FROM matches WHERE batch_id = %s AND product_id = ANY(%s)",
        (batch_id, bare),
    ).fetchone()
    assert offered == (0,)
