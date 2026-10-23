"""HMAC-SHA256 session tokens.

Replaces the bare session-ID-as-bearer pattern with a short-lived signed token.

Token format (dot-separated, 3 parts):
    <session_id>.<exp_unix_int>.<hmac_hex>

The HMAC is computed over "<session_id>:<exp_unix_int>" using the
ADMIN_API_KEY as the secret (already a required env var — no new secret needed).

Usage
-----
    from maneki.services.token import issue_token, verify_token

    # At POST /sessions:
    token = issue_token(session_id, ttl_hours=6)

    # At every protected endpoint:
    session_id = verify_token(token)   # raises UnauthorizedError if invalid/expired
"""
from __future__ import annotations

import hashlib
import hmac
import time
from uuid import UUID

from maneki.config import get_settings
from maneki.errors import UnauthorizedError

from datetime import datetime

# Allow up to 30 seconds of clock skew past the token's own expiry
_CLOCK_SKEW_S = 30


def _secret() -> bytes:
    """Return the HMAC secret for session signing.

    Prefers SESSION_TOKEN_SECRET if configured; falls back to ADMIN_API_KEY.
    """
    cfg = get_settings()
    key = cfg.session_token_secret or cfg.admin_api_key
    return key.encode()


def issue_token(
    session_id: UUID | str,
    session_expires_at: datetime | str | None = None,
    ttl_hours: int | None = None,
) -> str:
    """Issue a signed session token.

    Args:
        session_id: The session UUID.
        session_expires_at: Optional database session expiration timestamp.
            The token exp will be capped at this timestamp so tokens never outlive
            the active session record in Postgres.
        ttl_hours: Token lifetime in hours. Defaults to SESSION_TTL_HOURS setting.

    Returns:
        A dot-separated token string: ``<sid>.<exp>.<hmac>``.
    """
    cfg = get_settings()
    hours = ttl_hours if ttl_hours is not None else cfg.session_ttl_hours
    exp = int(time.time()) + hours * 3600

    if session_expires_at is not None:
        if isinstance(session_expires_at, str):
            try:
                dt = datetime.fromisoformat(session_expires_at)
                exp = min(exp, int(dt.timestamp()))
            except ValueError:
                pass
        elif isinstance(session_expires_at, datetime):
            exp = min(exp, int(session_expires_at.timestamp()))

    sid = str(session_id)
    payload = f"{sid}:{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{sid}.{exp}.{sig}"


def verify_token(token: str | None) -> UUID:
    """Verify a session token and return the validated session UUID.

    Args:
        token: Raw token string (without the ``Bearer `` prefix).

    Returns:
        The validated session :class:`~uuid.UUID`.

    Raises:
        UnauthorizedError: Token is missing, malformed, expired, or signature
            is invalid.
    """
    if not token:
        raise UnauthorizedError("Authorization token is required")

    parts = token.split(".")
    if len(parts) != 3:  # noqa: PLR2004
        raise UnauthorizedError("Malformed session token")

    sid_str, exp_str, sig = parts

    try:
        exp = int(exp_str)
    except ValueError:
        raise UnauthorizedError("Malformed session token")

    if time.time() > exp + _CLOCK_SKEW_S:
        raise UnauthorizedError("Session token has expired")

    # Constant-time HMAC comparison (prevents timing attacks)
    payload = f"{sid_str}:{exp_str}"
    expected_sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise UnauthorizedError("Invalid session token signature")

    try:
        return UUID(sid_str)
    except ValueError:
        raise UnauthorizedError("Malformed session token")
