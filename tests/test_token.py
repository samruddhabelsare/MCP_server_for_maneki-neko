"""Tests for HMAC-SHA256 session token issuance and verification."""
import time
from uuid import uuid4

import pytest

from maneki.config import get_settings
from maneki.errors import UnauthorizedError
from maneki.services.token import issue_token, verify_token


def test_issue_and_verify_token_roundtrip() -> None:
    sid = uuid4()
    token = issue_token(sid, ttl_hours=2)
    assert isinstance(token, str)
    assert token.count(".") == 2

    verified_sid = verify_token(token)
    assert verified_sid == sid


def test_verify_token_tampered_payload_rejected() -> None:
    sid = uuid4()
    token = issue_token(sid)
    parts = token.split(".")

    # Tamper with the session id in the payload
    fake_sid = str(uuid4())
    tampered = f"{fake_sid}.{parts[1]}.{parts[2]}"

    with pytest.raises(UnauthorizedError, match="Invalid session token signature"):
        verify_token(tampered)


def test_verify_token_tampered_signature_rejected() -> None:
    sid = uuid4()
    token = issue_token(sid)
    parts = token.split(".")

    tampered = f"{parts[0]}.{parts[1]}.deadbeefdeadbeefdeadbeefdeadbeef"
    with pytest.raises(UnauthorizedError, match="Invalid session token signature"):
        verify_token(tampered)


def test_verify_token_expired_rejected() -> None:
    sid = uuid4()
    # Issue with negative TTL to simulate expired token
    token = issue_token(sid, ttl_hours=-1)

    with pytest.raises(UnauthorizedError, match="Session token has expired"):
        verify_token(token)


def test_verify_token_malformed() -> None:
    with pytest.raises(UnauthorizedError):
        verify_token("")

    with pytest.raises(UnauthorizedError, match="Malformed"):
        verify_token("invalid.token")

    with pytest.raises(UnauthorizedError, match="Malformed"):
        verify_token(f"{uuid4()}.notanint.sig")


def test_token_capped_at_session_expires_at() -> None:
    from datetime import datetime, timedelta, timezone
    sid = uuid4()
    # Session expires in 30 minutes
    session_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    # Request 6 hours TTL, but expect cap at 30 minutes
    token = issue_token(sid, session_expires_at=session_expiry, ttl_hours=6)
    parts = token.split(".")
    token_exp = int(parts[1])
    assert abs(token_exp - int(session_expiry.timestamp())) <= 2


def test_token_uses_dedicated_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid4()
    monkeypatch.setenv("SESSION_TOKEN_SECRET", "super-secret-session-key-456")
    get_settings.cache_clear()

    token = issue_token(sid)
    verified = verify_token(token)
    assert verified == sid

    # Now change secret — previous token must fail signature check
    monkeypatch.setenv("SESSION_TOKEN_SECRET", "different-secret-key-789")
    get_settings.cache_clear()

    with pytest.raises(UnauthorizedError, match="Invalid session token signature"):
        verify_token(token)

    get_settings.cache_clear()
