"""The four Lambda handlers, offline: moto for S3 and SSM, a scratch Postgres as lambda_app.

The stdlib SigV4 signer (common.sigv4) must produce exactly what botocore produces, since the
presign and status Lambdas use it instead of importing boto3. Everything AWS-facing runs under
moto with fake credentials, so no test can reach a real account.
"""

import base64
import datetime as dt
import http.server
import io
import json
import threading
import time
from collections.abc import Iterator
from typing import Any, ClassVar

import boto3
import botocore.auth
import botocore.config
import botocore.signers
import psycopg
import pytest
from moto import mock_aws

from common import db
from common.sigv4 import Credentials, presigned_post, rds_auth_token
from common.webhook_jwt import verify_token
from scripts import migrate
from scripts.gen_users import dirty, generate
from tests.test_ingest import as_csv

REGION = "ap-southeast-2"
FIXED = dt.datetime(2026, 10, 17, 12, 0, 0)
CREDS = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "session-token")
MAX = 50 * 1024 * 1024


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fake credentials and moto, so boto3 calls stay in-process."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION,
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


@pytest.fixture
def database(
    scratch_db: str, local_settings: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> str:
    """A migrated scratch database the handlers reach through common.db, as lambda_app."""
    migrate.run("local", scratch_db)
    for key, value in {
        "DB_AUTH": "password", "DB_HOST": "127.0.0.1",
        "DB_PORT": local_settings["APP_DB_HOST_PORT"], "DB_NAME": scratch_db,
        "DB_USER": "lambda_app", "DB_PASSWORD": local_settings["LAMBDA_APP_DB_PASSWORD"],
        "DB_SSLMODE": "disable",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db, "_conn", None)
    return scratch_db


# --- SigV4 ---------------------------------------------------------------------------------------


def freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (botocore.auth, botocore.signers):
        monkeypatch.setattr(module, "get_current_datetime", lambda *a, **k: FIXED)


def client(service: str) -> Any:
    # SigV4 explicitly: left to itself, botocore still signs S3 POST policies the legacy (v2) way.
    return boto3.client(service, region_name=REGION, aws_access_key_id=CREDS.access_key,
                        aws_secret_access_key=CREDS.secret_key,
                        aws_session_token=CREDS.session_token,
                        config=botocore.config.Config(
                            signature_version="s3v4" if service == "s3" else None))  # fmt: skip


def test_the_presigned_post_equals_botocores(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch)
    theirs = client("s3").generate_presigned_post(
        Bucket="uploads-bucket", Key="uploads/x.csv", Fields={"Content-Type": "text/csv"},
        Conditions=[{"Content-Type": "text/csv"}, ["content-length-range", 1, MAX]],
        ExpiresIn=300,
    )  # fmt: skip
    ours = presigned_post(bucket="uploads-bucket", key="uploads/x.csv", region=REGION,
                          credentials=CREDS, content_type="text/csv", max_bytes=MAX,
                          expires_in=300, now=FIXED)  # fmt: skip
    # Same fields, policy and signature. The URL is the regional endpoint, which AWS recommends
    # for browser POSTs (no redirect, which would break the cross-origin upload).
    assert ours["fields"] == theirs["fields"]
    assert ours["url"] == f"https://uploads-bucket.s3.{REGION}.amazonaws.com/"


def test_the_rds_auth_token_equals_botocores(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch)
    host = "loans.abc123.ap-southeast-2.rds.amazonaws.com"
    theirs = client("rds").generate_db_auth_token(
        DBHostname=host, Port=5432, DBUsername="lambda_app", Region=REGION
    )
    ours = rds_auth_token(host=host, port=5432, user="lambda_app", region=REGION,
                          credentials=CREDS, now=FIXED)  # fmt: skip
    assert ours == theirs


# --- presign -------------------------------------------------------------------------------------


def test_presign_returns_a_post_for_one_new_key_and_refuses_bad_sizes(
    monkeypatch: pytest.MonkeyPatch, aws: None
) -> None:
    from presign.handler import handler

    monkeypatch.setenv("UPLOAD_BUCKET", "uploads-bucket")
    ok = handler({"body": json.dumps({"filename": "u.csv", "size": 1000})}, None)
    target = json.loads(ok["body"])
    assert ok["statusCode"] == 200
    assert target["url"] == f"https://uploads-bucket.s3.{REGION}.amazonaws.com/"
    key = target["fields"]["key"]
    assert key.startswith("uploads/")
    assert key.endswith(".csv")
    assert target["fields"]["Content-Type"] == "text/csv"
    encoded = base64.b64encode(json.dumps({"size": 10}).encode()).decode()
    assert handler({"body": encoded, "isBase64Encoded": True}, None)["statusCode"] == 200
    for body in ({"size": 0}, {"size": MAX + 1}, "not json"):
        bad = handler({"body": body if isinstance(body, str) else json.dumps(body)}, None)
        assert bad["statusCode"] == 400


# --- ingest --------------------------------------------------------------------------------------


def s3_event(bucket: str, key: str, etag: str) -> dict[str, Any]:
    return {"Records": [{"s3": {"bucket": {"name": bucket},
                                "object": {"key": key, "eTag": f'"{etag}"'}}}]}  # fmt: skip


def test_ingest_reads_from_s3_writes_rejects_and_ignores_a_redelivery(
    aws: None, database: str
) -> None:
    from ingest.handler import handler

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="uploads-test", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    rows, _ = dirty(generate(500, 8, "real"), 0.1, 8)
    put = s3.put_object(Bucket="uploads-test", Key="uploads/a.csv", Body=as_csv(rows))
    event = s3_event("uploads-test", "uploads/a.csv", put["ETag"].strip('"'))

    result = handler(event, None)
    assert result["duplicate"] is False
    assert result["rows_valid"] + result["rows_rejected"] == 500
    assert result["rows_rejected"] > 0
    rejects = (
        s3.get_object(Bucket="uploads-test", Key=result["rejects_key"])["Body"].read().decode()
    )
    assert rejects.startswith("line,reason,")
    assert handler(event, None)["duplicate"] is True


def test_ingest_records_a_file_it_cannot_read_as_a_failed_batch(aws: None, database: str) -> None:
    from ingest.handler import handler

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="uploads-test", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    s3.put_object(Bucket="uploads-test", Key="uploads/b.csv", Body=b"id,name\r\n1,x\r\n")
    result = handler(s3_event("uploads-test", "uploads/b.csv", "e"), None)
    assert result["failed"] is True
    assert "missing column" in result["error"]


# --- notify --------------------------------------------------------------------------------------


class Receiver(http.server.BaseHTTPRequestHandler):
    """Stands in for webhook-gw: records the request and answers with a chosen status."""

    seen: ClassVar[list[dict[str, Any]]] = []
    status = 202

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.seen.append({"path": self.path, "auth": self.headers["Authorization"],
                              "body": json.loads(body)})  # fmt: skip
        self.send_response(Receiver.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def receiver() -> Iterator[str]:
    Receiver.seen, Receiver.status = [], 202
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def test_notify_signs_the_webhook_with_the_ssm_secret_and_skips_duplicates(
    monkeypatch: pytest.MonkeyPatch, aws: None, receiver: str
) -> None:
    from notify.handler import handler

    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/loan/tunnel", Value=receiver, Type="String")
    ssm.put_parameter(Name="/loan/secret", Value="s3cret-value", Type="SecureString")
    monkeypatch.setenv("TUNNEL_URL_PARAM", "/loan/tunnel")
    monkeypatch.setenv("WEBHOOK_SECRET_PARAM", "/loan/secret")
    payload = {"batch_id": "b-1", "rows_valid": 9, "rows_rejected": 1, "duplicate": False}

    assert handler({"responsePayload": payload}, None) == {"batch_id": "b-1", "status": 202}
    (request,) = Receiver.seen
    assert request["path"] == "/webhook/loan-match"
    claims = verify_token(request["auth"].removeprefix("Bearer "), "s3cret-value")
    assert (claims["batch_id"], claims["rows_valid"]) == ("b-1", 9)

    assert handler({"responsePayload": {**payload, "duplicate": True}}, None)["skipped"] is True
    assert len(Receiver.seen) == 1
    Receiver.status = 500
    with pytest.raises(RuntimeError, match="HTTP 500"):
        handler({"responsePayload": payload}, None)


# --- status --------------------------------------------------------------------------------------


def test_status_answers_for_an_upload_key(database: str) -> None:
    from ingest.core import ingest
    from status.handler import handler

    conn = db.connect()
    data = io.BytesIO(as_csv(generate(10, 9, "real")))
    ingest(conn, data, bucket="uploads-test", key="uploads/c.csv",
           etag="e")  # fmt: skip
    ok = handler({"queryStringParameters": {"key": "uploads/c.csv"}}, None)
    body = json.loads(ok["body"])
    assert (ok["statusCode"], body["status"], body["rows"]["valid"]) == (200, "ingested", 10)
    assert handler({"queryStringParameters": {"key": "../etc"}}, None)["statusCode"] == 400
    conn2 = db.connect()
    assert conn2 is conn  # the container's connection is reused between invocations
    assert isinstance(conn2, psycopg.Connection)


# --- Database logins (common.db, DECISIONS AD41) -------------------------------------------------


@pytest.fixture
def quick_hedges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real schedule in tenths of a second, so the timing tests run fast."""
    monkeypatch.setattr(db, "HEDGE_AFTER", (0.3, 0.6, 0.9))
    monkeypatch.setattr(db, "RETRY_PAUSE", 0.05)


def test_a_slow_login_is_waited_for_not_cancelled(
    database: str, monkeypatch: pytest.MonkeyPatch, quick_hedges: None
) -> None:
    """A login slower than the first hedge must still be the one that wins.

    The incident: a cold IAM check took longer than each short attempt, and cancelling it
    restarted the check.
    """
    real = psycopg.connect
    calls: list[int] = []

    def slow_then_ok(**kwargs: Any) -> psycopg.Connection:
        calls.append(kwargs["connect_timeout"])
        time.sleep(0.4)  # past the first hedge (0.3 s)
        return real(**kwargs)

    monkeypatch.setattr(psycopg, "connect", slow_then_ok)
    conn = db.connect(budget=5)
    assert conn.execute("SELECT 1").fetchone() == (1,)
    assert calls[0] >= 4  # the first attempt may wait for (nearly) the whole budget


def test_a_stuck_login_is_hedged_and_the_late_one_closed(
    database: str, monkeypatch: pytest.MonkeyPatch, quick_hedges: None
) -> None:
    real = psycopg.connect
    opened: list[psycopg.Connection] = []
    lock = threading.Lock()

    def first_stuck(**kwargs: Any) -> psycopg.Connection:
        with lock:
            first = not opened and not getattr(first_stuck, "seen", False)
            first_stuck.seen = True  # type: ignore[attr-defined]
        if first:
            time.sleep(1.2)  # a stuck path: the hedge started at 0.3 s finishes long before
        conn = real(**kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", first_stuck)
    started = time.monotonic()
    conn = db.connect(budget=5)
    assert time.monotonic() - started < 1.0  # the hedge won; nobody waited for the stuck one
    assert conn.execute("SELECT 1").fetchone() == (1,)
    time.sleep(1.5)  # the stuck attempt finishes late, sees it lost, and closes itself
    late = [c for c in opened if c is not conn]
    assert late
    assert all(c.closed for c in late)
    assert not conn.closed


def test_fast_failures_retry_until_the_budget_then_raise(
    database: str, monkeypatch: pytest.MonkeyPatch, quick_hedges: None
) -> None:
    calls: list[float] = []

    def refused(**kwargs: Any) -> psycopg.Connection:
        calls.append(time.monotonic())
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refused)
    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError, match="no database login within"):
        db.connect(budget=0.6)
    assert 0.5 < time.monotonic() - started < 1.5
    assert len(calls) > 3  # RDS still starting: tried again and again, not given up at once


def test_a_dead_cached_connection_is_replaced(
    database: str, local_settings: dict[str, str]
) -> None:
    from scripts.dbconn import admin_connect_kwargs

    conn = db.connect()
    pid = conn.info.backend_pid
    with psycopg.connect(**admin_connect_kwargs(local_settings, "local", database),
                         autocommit=True) as admin:  # fmt: skip
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    again = db.connect()
    assert again is not conn
    assert again.execute("SELECT 1").fetchone() == (1,)


def test_warm_ups_log_in_and_never_raise(database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.handler import handler as ingest_handler
    from notify.handler import handler as notify_handler
    from status.handler import handler as status_handler

    first = ingest_handler({"warmup": True}, None)
    assert (first["warm"], first["reused"]) == (True, False)
    assert status_handler({"warmup": True}, None)["reused"] is True  # same container connection
    # Through ingest's onSuccess destination a ping reaches notify, which skips it.
    assert notify_handler({"responsePayload": first}, None)["skipped"] is True

    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setenv("DB_PORT", "1")  # nothing listens: a stopped database
    started = time.monotonic()
    down = status_handler({"warmup": True}, None)
    assert down["warm"] is False
    assert time.monotonic() - started < 4  # the TCP check answers at once; no minutes billed


def test_the_login_budget_follows_the_lambdas_remaining_time() -> None:
    class Context:
        def __init__(self, ms: int) -> None:
            self.ms = ms

        def get_remaining_time_in_millis(self) -> int:
            return self.ms

    assert db.budget_for(Context(300_000), reserve=60, cap=120) == 120
    assert db.budget_for(Context(90_000), reserve=60, cap=120) == 30
    assert db.budget_for(Context(10_000), reserve=60, cap=120) == 5  # never below 5 s
    assert db.budget_for(None, reserve=60, cap=120) == db.DEFAULT_BUDGET


def test_presign_can_pin_gzip_for_a_compressed_upload(
    monkeypatch: pytest.MonkeyPatch, aws: None
) -> None:
    from presign.handler import handler

    monkeypatch.setenv("UPLOAD_BUCKET", "uploads-bucket")
    zipped = json.loads(handler({"body": json.dumps({"size": 10, "gzip": True})}, None)["body"])
    plain = json.loads(handler({"body": json.dumps({"size": 10})}, None)["body"])
    assert zipped["fields"]["Content-Encoding"] == "gzip"
    policy = json.loads(base64.b64decode(zipped["fields"]["policy"]))
    assert {"Content-Encoding": "gzip"} in policy["conditions"]
    assert "Content-Encoding" not in plain["fields"]
