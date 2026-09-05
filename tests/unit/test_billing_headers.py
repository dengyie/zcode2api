"""billing 族请求头与 JWT user_id 解析单测（zcode-switch 移植，2026-09-06）。

_auth_headers 是全部 billing 上游流量的头出口，形状漂移 = 上游资格/风控判定
漂移，逐字段锁定；jwt_user_id 是激活事件上报的用户标识来源。
"""

from __future__ import annotations

import base64
import json

import pytest

from app import constants, settings
from app.claim import jwt_user_id
from app.models import Account
from app.quota import _auth_headers


def _make_jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h1.{seg}.sig"


@pytest.fixture
def acc(monkeypatch, tmp_path) -> Account:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)  # device_mid 落盘隔离
    return Account(id="a1", name="n", provider="zai", mode="jwt",
                   jwt_token=_make_jwt({"user_id": "u-uuid", "sub": "s-uuid"}))


class TestJwtUserId:
    def test_prefers_user_id(self, acc):
        assert jwt_user_id(acc) == "u-uuid"

    def test_falls_back_to_sub(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
        acc2 = Account(id="a2", name="n", provider="zai", mode="jwt",
                       jwt_token=_make_jwt({"sub": "s-uuid"}))
        assert jwt_user_id(acc2) == "s-uuid"

    def test_malformed_token_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
        for bad in ("", "not-a-jwt", "h1.!!!not-base64!!!.sig"):
            acc3 = Account(id="a3", name="n", provider="zai", mode="jwt", jwt_token=bad)
            assert jwt_user_id(acc3) is None, bad

    def test_empty_token_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
        acc4 = Account(id="a4", name="n", provider="zai", mode="jwt", jwt_token=None)
        assert jwt_user_id(acc4) is None


class TestAuthHeaders:
    def test_jwt_mode_full_shape(self, acc):
        h = _auth_headers(acc)
        assert h["User-Agent"] == f"ZCode/{constants.BILLING_APP_VERSION}"
        assert h["X-ZCode-App-Version"] == constants.BILLING_APP_VERSION
        assert h["HTTP-Referer"] == constants.ZCODE_ORIGIN
        assert h["X-Title"] == constants.BILLING_TITLE
        assert h["X-Platform"] == constants.CLIENT_PLATFORM
        assert h["X-Release-Channel"] == constants.BILLING_RELEASE_CHANNEL
        assert h["X-Client-Language"] == constants.IDENTITY_CLIENT_LANGUAGE
        assert h["X-Client-Timezone"] == constants.IDENTITY_CLIENT_TIMEZONE
        assert h["X-Os-Category"] == constants.IDENTITY_OS_CATEGORY
        assert h["X-Os-Version"] == constants.IDENTITY_OS_VERSION
        assert h["X-Device-Mid"]
        assert h["Authorization"] == f"Bearer {acc.jwt_token}"
        # 每请求全新 uuid
        assert _auth_headers(acc)["x-request-id"] != h["x-request-id"]

    def test_api_key_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
        acc2 = Account(id="a5", name="n", provider="zai", mode="apiKey", api_key="sk-x")
        h = _auth_headers(acc2)
        assert h["x-api-key"] == "sk-x"
        assert "Authorization" not in h
