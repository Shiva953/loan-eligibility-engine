"""Tests for scripts/profile_users.py.

The real-data tests pin the facts that docs/PLAN.md section 2 relies on, so a changed sample
file fails loudly here instead of silently invalidating the design. The synthetic tests check
that each validity rule fires, because the ingest validator will copy these rules.
"""

from pathlib import Path

import pytest

from scripts.profile_users import (
    MATCHING_COLUMNS,
    Profile,
    detect_line_endings,
    main,
    profile_csv,
    render_markdown,
)

REPO = Path(__file__).resolve().parent.parent
USERS_CSV = REPO / "data" / "users.csv"
HEADER = "user_id,name,email,monthly_income,credit_score,employment_status,age"


@pytest.fixture(scope="module")
def real() -> Profile:
    return profile_csv(USERS_CSV)


def test_real_file_shape(real: Profile) -> None:
    assert real.rows == 10_000
    assert real.dirty_rows == 0
    assert real.line_endings == "CRLF"
    assert not real.has_bom
    assert dict(real.field_counts) == {7: 10_000}


def test_real_identity_columns(real: Profile) -> None:
    assert real.uuid_versions == {4: 10_000}
    assert real.distinct_emails == 2_632
    assert real.max_users_per_email == 12


def test_real_value_ranges(real: Profile) -> None:
    income = real.numeric["monthly_income"]
    score = real.numeric["credit_score"]
    age = real.numeric["age"]
    assert (income.minimum, income.maximum, income.step) == (25_000, 150_000, 1_000)
    assert (score.minimum, score.maximum, score.step) == (650, 800, 5)
    assert (age.minimum, age.maximum) == (22, 50)
    assert real.employment == {"Salaried": 3_389, "Self-Employed": 3_371, "Business": 3_240}


def test_real_cohort_cardinality(real: Profile) -> None:
    assert real.cohort_cardinality[("employment_status",)] == 3
    assert real.cohort_cardinality[("employment_status", "age")] == 87
    assert real.cohort_cardinality[MATCHING_COLUMNS] == 9_869
    assert all(abs(r) < 0.05 for r in real.correlations.values())


def write_csv(tmp_path: Path, *rows: str, bom: bool = False) -> Path:
    path = tmp_path / "users.csv"
    body = "\r\n".join([HEADER, *rows]) + "\r\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode())
    return path


GOOD = "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f60,Asha Rao,asha.rao@example.com,50000,720,Salaried,30"


@pytest.mark.parametrize(
    ("row", "rule"),
    [
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com,,720,Salaried,30",
            "missing_value",
        ),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com, 50000,720,Salaried,30",
            "padded_value",
        ),
        ("not-a-uuid,A B,a@example.com,50000,720,Salaried,30", "bad_user_id"),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,not-an-email,50000,720,Salaried,30",
            "bad_email",
        ),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com,50k,720,Salaried,30",
            "non_integer",
        ),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com,50000,999,Salaried,30",
            "out_of_range",
        ),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com,50000,720,Student,30",
            "unknown_employment",
        ),
        (
            "6f1c2a1e-0d5b-4c8e-9a3f-1b2c3d4e5f61,A B,a@example.com,50000,720,Salaried",
            "wrong_field_count",
        ),
        (GOOD, "duplicate_user_id"),
    ],
)
def test_each_rule_fires(tmp_path: Path, row: str, rule: str) -> None:
    profile = profile_csv(write_csv(tmp_path, GOOD, row))
    assert profile.violations[rule] == 1
    assert profile.dirty_rows == 1
    assert profile.clean_rows == 1


def test_bom_is_detected_and_ignored(tmp_path: Path) -> None:
    profile = profile_csv(write_csv(tmp_path, GOOD, bom=True))
    assert profile.has_bom
    assert profile.header[0] == "user_id"
    assert profile.clean_rows == 1


def test_line_endings() -> None:
    assert detect_line_endings("a\r\nb\r\n") == "CRLF"
    assert detect_line_endings("a\nb\n") == "LF"
    assert detect_line_endings("a\r\nb\n") == "mixed"


def test_render_is_deterministic(real: Profile) -> None:
    assert render_markdown(real) == render_markdown(profile_csv(USERS_CSV))


def test_check_mode_detects_stale_report(tmp_path: Path) -> None:
    out = tmp_path / "PROFILE.md"
    csv_path = write_csv(tmp_path, GOOD)
    assert main([str(csv_path), "--out", str(out), "--check"]) == 1
    assert main([str(csv_path), "--out", str(out)]) == 0
    assert main([str(csv_path), "--out", str(out), "--check"]) == 0
