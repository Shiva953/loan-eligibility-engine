"""Unit tests for the phase 1 scripts.

Covers env handling, config validation, credentials, workflow building and drift detection.
None of them need Docker.
"""

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from scripts import build_workflows, export_workflows, make_env, seed_config
from scripts.envfile import REPO_ROOT, parse_env_text
from scripts.n8n_credentials import build_credentials
from scripts.n8n_ids import CREDENTIALS, WORKFLOWS

# --- envfile ---------------------------------------------------------------------------------


def test_parse_env_text() -> None:
    text = "# comment\n\nA=1\nB = two words (x; y)\nC='quoted'\nD=\nnot a pair\n"
    assert parse_env_text(text) == {"A": "1", "B": "two words (x; y)", "C": "quoted", "D": ""}


# --- make_env --------------------------------------------------------------------------------


def test_make_env_fills_blanks_and_never_overwrites(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("N8N_ENCRYPTION_KEY=\nN8N_DB_PASSWORD=\nGEMINI_API_KEY=\nPORT=5678\n")
    env = tmp_path / ".env"
    env.write_text("N8N_ENCRYPTION_KEY=keep-me\nN8N_DB_PASSWORD=\nGEMINI_API_KEY=\n")

    generated, added = make_env.complete_env(env, example)

    values = parse_env_text(env.read_text())
    assert values["N8N_ENCRYPTION_KEY"] == "keep-me"
    assert len(values["N8N_DB_PASSWORD"]) == 64
    assert values["GEMINI_API_KEY"] == ""  # owner-supplied, never generated
    assert values["PORT"] == "5678"
    assert generated == ["N8N_DB_PASSWORD"]
    assert added == ["PORT"]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600

    before = env.read_text()
    make_env.complete_env(env, example)
    assert env.read_text() == before


def test_make_env_creates_from_example(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("WEBHOOK_HMAC_SECRET=\n")
    env = tmp_path / ".env"
    make_env.complete_env(env, example)
    assert len(parse_env_text(env.read_text())["WEBHOOK_HMAC_SECRET"]) == 64


# --- seed_config -----------------------------------------------------------------------------


def valid_settings() -> dict[str, str]:
    return parse_env_text((REPO_ROOT / ".env.example").read_text())


def test_example_env_passes_config_validation() -> None:
    config = seed_config.build_config(valid_settings())
    assert config["email_mode"] == "suppressed"
    assert config["email_aliases"] == "shivaset2+1@gmail.com,shivaset2+2@gmail.com"
    assert not any("key" in k or "secret" in k or "password" in k for k in config)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("EMAIL_CAP", "3", "EMAIL_CAP"),
        ("EMAIL_CAP", "0", "EMAIL_CAP"),
        ("EMAIL_MODE", "live", "EMAIL_MODE"),
        ("EMAIL_ALIASES", "not-an-email", "EMAIL_ALIASES"),
        ("CRAWL_MODE", "both", "CRAWL_MODE"),
        ("LLM_RPM", "-1", "LLM_RPM"),
        ("EXTRACTION_CONFIDENCE_MIN", "1.5", "EXTRACTION_CONFIDENCE_MIN"),
    ],
)
def test_config_validation_rejects(key: str, value: str, message: str) -> None:
    settings = valid_settings()
    settings[key] = value
    with pytest.raises(SystemExit, match=message):
        seed_config.build_config(settings)


# --- n8n credentials -------------------------------------------------------------------------


def cred_settings(**overrides: str) -> dict[str, str]:
    settings = {
        "N8N_APP_DB_PASSWORD": "pw",
        "WEBHOOK_HMAC_SECRET": "s3cret",
        "GEMINI_API_KEY": "g",
        "AWS_REGION": "ap-southeast-2",
    }
    settings.update(overrides)
    return settings


def test_credentials_payload_shape() -> None:
    payload = build_credentials(cred_settings(), "local", forbidden_key_ids=set())
    assert {c["id"] for c in payload} == {ref["id"] for ref in CREDENTIALS.values()}
    by_type = {c["type"]: c["data"] for c in payload}
    assert by_type["postgres"]["host"] == "app-db"
    assert by_type["postgres"]["user"] == "n8n_app"
    assert by_type["jwtAuth"] == {"keyType": "passphrase", "secret": "s3cret", "algorithm": "HS256"}
    assert by_type["httpHeaderAuth"]["name"] == "x-goog-api-key"
    assert by_type["aws"]["accessKeyId"] == ""


def test_admin_aws_key_is_refused() -> None:
    settings = cred_settings(SES_AWS_ACCESS_KEY_ID="AKIAADMIN", SES_AWS_SECRET_ACCESS_KEY="x")
    with pytest.raises(SystemExit, match="admin keys"):
        build_credentials(settings, "local", forbidden_key_ids={"AKIAADMIN"})


def test_ses_key_needs_both_halves() -> None:
    settings = cred_settings(SES_AWS_ACCESS_KEY_ID="AKIASES")
    with pytest.raises(SystemExit, match="both"):
        build_credentials(settings, "local", forbidden_key_ids=set())


def test_rds_target_uses_verified_tls() -> None:
    settings = cred_settings(RDS_HOST="db.example", RDS_N8N_APP_PASSWORD="r")
    payload = build_credentials(settings, "rds", forbidden_key_ids=set())
    postgres = next(c["data"] for c in payload if c["type"] == "postgres")
    assert postgres["ssl"] == "require"
    assert postgres["allowUnauthorizedCerts"] is False


# --- build_workflows -------------------------------------------------------------------------


def test_repo_templates_build() -> None:
    outputs = build_workflows.build_all()
    assert set(outputs) == {f"{stem}.json" for stem in WORKFLOWS}
    for text in outputs.values():
        assert "@@" not in text


def write_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "c_notification.json").write_text(json.dumps(body))
    monkeypatch.setattr(build_workflows, "SRC_DIR", src)
    return src


def minimal(nodes: list[dict[str, Any]], connections: dict[str, Any]) -> dict[str, Any]:
    return {"name": "t", "nodes": nodes, "connections": connections, "settings": {}}


def node(name: str, **extra: Any) -> dict[str, Any]:
    return {"id": f"id-{name}", "name": name, "type": "n8n-nodes-base.noOp", **extra}


def test_placeholders_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = minimal(
        [
            node("a", credentials={"postgres": "@@credential:app_postgres"}),
            node("b", parameters={"query": "@@include:sql/common/load_config.sql"}),
            node("c", parameters={"workflowId": "@@workflow:llm_gateway"}),
        ],
        {"a": {"main": [[{"node": "b", "type": "main", "index": 0}]]}},
    )
    src = write_template(tmp_path, monkeypatch, body)
    built = json.loads(build_workflows.build_all(src)["c_notification.json"])
    assert built["id"] == WORKFLOWS["c_notification"]
    assert built["nodes"][0]["credentials"]["postgres"] == CREDENTIALS["app_postgres"]
    assert built["nodes"][1]["parameters"]["query"].lstrip().startswith("--")
    assert built["nodes"][2]["parameters"]["workflowId"] == WORKFLOWS["llm_gateway"]


@pytest.mark.parametrize(
    ("nodes", "connections", "message"),
    [
        ([node("a"), node("a")], {}, "duplicate node names"),
        ([node("a")], {"a": {"main": [[{"node": "ghost"}]]}}, "unknown node"),
        ([node("a", x="@@include:../.env")], {}, "outside"),
        ([node("a", x="@@credential:nope")], {}, "unknown credential"),
        ([node("a", x="@@mystery:x")], {}, "unknown placeholder"),
    ],
)
def test_build_rejects_bad_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nodes: list[dict[str, Any]],
    connections: dict[str, Any],
    message: str,
) -> None:
    src = write_template(tmp_path, monkeypatch, minimal(nodes, connections))
    with pytest.raises(build_workflows.BuildError, match=message):
        build_workflows.build_all(src)


def test_sql_with_expression_braces_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "bad.sql").write_text("-- {{ $json.x }}\nSELECT 1;\n")
    monkeypatch.setattr(build_workflows, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_workflows, "INCLUDE_ROOTS", (sql_dir,))
    src = write_template(tmp_path, monkeypatch, minimal([node("a", q="@@include:sql/bad.sql")], {}))
    with pytest.raises(build_workflows.BuildError, match="evaluate"):
        build_workflows.build_all(src)


# --- export drift ----------------------------------------------------------------------------


def test_drift_detection() -> None:
    built = {name: json.loads(text) for name, text in build_workflows.build_all().items()}
    exported = [
        {**wf, "active": True, "versionId": "v", "updatedAt": "now"} for wf in built.values()
    ]
    findings, _ = export_workflows.compare(built, exported)
    assert findings == []

    exported[0] = {**exported[0], "name": "renamed in UI"}
    exported.append({"id": "zzzzzzzzzzzzzzzz", "name": "scratch", "active": False})
    findings, by_file = export_workflows.compare(built, exported)
    assert any("content differs" in f for f in findings)
    assert any("exists only in n8n" in f for f in findings)
    assert "unmanaged-zzzzzzzzzzzzzzzz.json" in by_file
