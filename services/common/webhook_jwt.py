"""Sign and verify the batch-ready webhook as a compact HS256 JWT (docs/DECISIONS.md AD11).

Why a JWT instead of a custom HMAC header: HS256 is HMAC-SHA256 over the token, and the token
carries its own timestamps (``iat``/``exp``) and the batch facts as claims. n8n's Webhook node
verifies HS256 tokens natively against a stored credential, so the shared secret never has to
be readable by a Code node, written into workflow JSON, or logged in execution data. Workflow B
trusts only the verified claims, never the request body.

Standard library only, so the notify Lambda needs no extra dependency for it.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 300


class TokenError(ValueError):
    """The token is malformed, wrongly signed, or outside its validity window."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise TokenError("bad base64url segment") from error


def _compact_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_token(
    claims: dict[str, Any],
    secret: str,
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Return a signed token carrying ``claims`` plus ``iat``, ``exp`` and a unique ``jti``.

    A short ``exp`` bounds replay. Replaying a still-valid token is harmless anyway, because
    Workflow B ignores batches that have already left the ``ingested`` state.
    """
    if not secret:
        raise ValueError("empty signing secret")
    issued_at = int(time.time()) if now is None else now
    payload = {**claims, "iat": issued_at, "exp": issued_at + ttl_seconds, "jti": str(uuid.uuid4())}
    header = {"alg": ALGORITHM, "typ": "JWT"}
    signing_input = (
        f"{_b64url_encode(_compact_json(header))}.{_b64url_encode(_compact_json(payload))}"
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_b64url_encode(signature.digest())}"


def verify_token(token: str, secret: str, *, now: int | None = None) -> dict[str, Any]:
    """Check signature and expiry, then return the claims.

    n8n does this check itself in production. This copy lets the tests and local tools prove
    that tokens from ``sign_token`` are what a standard HS256 verifier accepts.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("token must have three segments")
    header = json.loads(_b64url_decode(parts[0]))
    if header.get("alg") != ALGORITHM:
        raise TokenError(f"unexpected algorithm {header.get('alg')!r}")
    expected = hmac.new(
        secret.encode("utf-8"), f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
        raise TokenError("bad signature")
    claims: dict[str, Any] = json.loads(_b64url_decode(parts[1]))
    current = int(time.time()) if now is None else now
    if not isinstance(claims.get("exp"), int) or current >= claims["exp"]:
        raise TokenError("token expired")
    return claims
