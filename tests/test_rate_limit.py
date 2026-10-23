"""Tests for proxy-aware IP and session-keyed rate limiting."""
from uuid import uuid4
from unittest.mock import MagicMock

from fastapi import Request

from maneki.rate_limit import get_chat_rate_limit_key, get_real_ip
from maneki.services.token import issue_token


def test_get_real_ip_with_x_forwarded_for() -> None:
    request = MagicMock(spec=Request)
    request.headers = {"x-forwarded-for": "198.51.100.42, 10.0.0.1, 172.16.0.1"}
    assert get_real_ip(request) == "198.51.100.42"


def test_get_real_ip_direct_client() -> None:
    request = MagicMock(spec=Request)
    request.headers = {}
    client_mock = MagicMock()
    client_mock.host = "192.0.2.1"
    request.client = client_mock
    assert get_real_ip(request) == "192.0.2.1"


def test_get_chat_rate_limit_key_from_token() -> None:
    sid = uuid4()
    token = issue_token(sid)

    request = MagicMock(spec=Request)
    request.headers = {
        "authorization": f"Bearer {token}",
        "x-forwarded-for": "203.0.113.5",
    }
    request.path_params = {"id": str(uuid4())}  # Different from token
    # Token session ID takes precedence
    key = get_chat_rate_limit_key(request)
    assert key == f"sess:{sid}"


def test_get_chat_rate_limit_key_from_path() -> None:
    sid = uuid4()
    request = MagicMock(spec=Request)
    request.headers = {}
    request.path_params = {"id": str(sid)}

    key = get_chat_rate_limit_key(request)
    assert key == f"sess:{sid}"


def test_get_chat_rate_limit_key_fallback_to_ip() -> None:
    request = MagicMock(spec=Request)
    request.headers = {"x-forwarded-for": "198.51.100.99"}
    request.path_params = {}

    key = get_chat_rate_limit_key(request)
    assert key == "ip:198.51.100.99"
