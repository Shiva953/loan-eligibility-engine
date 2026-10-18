"""The webhook token must be a standard HS256 JWT, because n8n verifies it with jsonwebtoken.

The jwt.io reference token pins the signing primitive to the standard. An end-to-end check
against the running n8n is `make webhook-test` (docs/TESTING.md, phase 1).
"""

import hashlib
import hmac

import pytest

from common.webhook_jwt import TokenError, _b64url_encode, sign_token, verify_token

# The HS256 example from jwt.io: secret "your-256-bit-secret".
JWT_IO_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def test_signature_primitive_matches_reference() -> None:
    header_payload, _, signature = JWT_IO_TOKEN.rpartition(".")
    digest = hmac.new(b"your-256-bit-secret", header_payload.encode(), hashlib.sha256).digest()
    assert _b64url_encode(digest) == signature


def test_round_trip_carries_claims_and_window() -> None:
    token = sign_token({"batch_id": "b-1"}, "s", now=1_000, ttl_seconds=300)
    claims = verify_token(token, "s", now=1_100)
    assert claims["batch_id"] == "b-1"
    assert claims["exp"] - claims["iat"] == 300
    assert claims["jti"]


@pytest.mark.parametrize(
    ("secret", "now", "message"),
    [("wrong", 1_100, "bad signature"), ("s", 1_300, "expired"), ("s", 5_000, "expired")],
)
def test_rejections(secret: str, now: int, message: str) -> None:
    token = sign_token({"batch_id": "b-1"}, "s", now=1_000, ttl_seconds=300)
    with pytest.raises(TokenError, match=message):
        verify_token(token, secret, now=now)


def test_tampered_payload_is_rejected() -> None:
    header, payload, signature = sign_token({"batch_id": "a"}, "s", now=1_000).split(".")
    forged = sign_token({"batch_id": "b"}, "other", now=1_000).split(".")[1]
    with pytest.raises(TokenError):
        verify_token(f"{header}.{forged}.{signature}", "s", now=1_100)
    assert verify_token(f"{header}.{payload}.{signature}", "s", now=1_100)["batch_id"] == "a"


def test_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        sign_token({}, "")
