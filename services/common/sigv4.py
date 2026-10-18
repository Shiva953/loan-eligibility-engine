"""AWS Signature Version 4 for the two things the Lambdas sign offline, without boto3.

Why not boto3: the presign and status Lambdas sit on the upload page's critical path, and
`import boto3` plus creating a client adds a few hundred milliseconds to every cold start. What
they need are two offline signatures, no API call:
  * an S3 POST policy (the browser uploads straight to the bucket), and
  * an RDS IAM auth token (a presigned "connect" URL; docs/DECISIONS.md AD1).
Both are a few HMAC-SHA256 steps from the Lambda's own credentials. Tests check that the
results equal botocore's for the same inputs and time.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class Credentials:
    """An access key, its secret and (for a Lambda's role) the session token."""

    access_key: str
    secret_key: str
    session_token: str | None = None

    @classmethod
    def from_env(cls) -> Credentials:
        """The credentials Lambda puts in the environment for the function's role."""
        return cls(
            os.environ["AWS_ACCESS_KEY_ID"],
            os.environ["AWS_SECRET_ACCESS_KEY"],
            os.environ.get("AWS_SESSION_TOKEN") or None,
        )


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """The derived key: HMAC chain over date, region, service and 'aws4_request'."""
    k = _hmac(f"AWS4{secret_key}".encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _uri_encode(value: str) -> str:
    return quote(value, safe="-_.~")


def presigned_post(
    *,
    bucket: str,
    key: str,
    region: str,
    credentials: Credentials,
    content_type: str,
    max_bytes: int,
    expires_in: int = 300,
    now: dt.datetime | None = None,
    extra_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A POST target for one exact key: {url, fields}, as boto3's generate_presigned_post.

    extra_fields (such as Content-Encoding) are added to the form and pinned by the policy.
    """
    now = now or dt.datetime.now(dt.UTC)
    date = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    credential = f"{credentials.access_key}/{date}/{region}/s3/aws4_request"
    extra = extra_fields or {}
    fields: dict[str, str] = {"key": key, "Content-Type": content_type, **extra}
    # The order botocore uses (caller's conditions, bucket, key, then the signature's own), so the
    # policy, and with it the signature, is byte-identical to generate_presigned_post's.
    conditions: list[Any] = [
        {"Content-Type": content_type},
        *({name: value} for name, value in extra.items()),
        ["content-length-range", 1, max_bytes],
        {"bucket": bucket},
        {"key": key},
        {"x-amz-algorithm": ALGORITHM},
        {"x-amz-credential": credential},
        {"x-amz-date": amz_date},
    ]
    if credentials.session_token:
        conditions.append({"x-amz-security-token": credentials.session_token})
    expiration = (now + dt.timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy = base64.b64encode(
        json.dumps({"expiration": expiration, "conditions": conditions}).encode()
    ).decode()
    signature = hmac.new(
        signing_key(credentials.secret_key, date, region, "s3"), policy.encode(), hashlib.sha256
    ).hexdigest()
    fields.update({"x-amz-algorithm": ALGORITHM, "x-amz-credential": credential,
                   "x-amz-date": amz_date})  # fmt: skip
    if credentials.session_token:
        fields["x-amz-security-token"] = credentials.session_token
    fields.update({"policy": policy, "x-amz-signature": signature})
    return {"url": f"https://{bucket}.s3.{region}.amazonaws.com/", "fields": fields}


def rds_auth_token(
    *,
    host: str,
    port: int,
    user: str,
    region: str,
    credentials: Credentials,
    expires_in: int = 900,
    now: dt.datetime | None = None,
) -> str:
    """The password for an IAM-authenticated RDS connection, as generate_db_auth_token."""
    now = now or dt.datetime.now(dt.UTC)
    date = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date}/{region}/rds-db/aws4_request"
    query = {
        "Action": "connect",
        "DBUser": user,
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{credentials.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_in),
        "X-Amz-SignedHeaders": "host",
    }
    if credentials.session_token:
        query["X-Amz-Security-Token"] = credentials.session_token

    def encoded(pairs: list[tuple[str, str]]) -> str:
        return "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in pairs)

    canonical_query = encoded(sorted(query.items()))
    netloc = f"{host}:{port}"
    canonical_request = "\n".join(
        ["GET", "/", canonical_query, f"host:{netloc}\n", "host", EMPTY_SHA256]
    )
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(credentials.secret_key, date, region, "rds-db"),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    # Signed over the sorted query; written in the order botocore writes it (same token).
    return f"{netloc}/?{encoded(list(query.items()))}&X-Amz-Signature={signature}"
