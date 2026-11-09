"""Coding 平面额度（api.z.ai monitor 端点）单元测试。

与 billing 族是两套独立账本：Plan 通道额度看 `app.quota.fetch_quota`，
API Key 直连 api.z.ai 的额度看 `fetch_coding_limits`。体验套餐号实测返回
`{"code":500,"msg":"当前用户不存在coding plan"}` —— 必须报成错误而不是空表，
否则 UI 会显示「额度 0」误导。
"""

from __future__ import annotations

import pytest

from app import quota
from app.models import Account


def _acc(api_key: str = "sk-test", **kw) -> Account:
    return Account(id="acc-1", name="acc", provider="zai", mode="apikey",
                   api_key=api_key, **kw)


class _FakeResp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """替换 quota.httpx.AsyncClient：记录请求头，回放固定响应。"""

    last_headers: dict = {}
    response: _FakeResp = _FakeResp({})

    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kw):
        type(self).last_headers = dict(headers or {})
        type(self).last_url = url
        return type(self).response


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(quota.httpx, "AsyncClient", _FakeClient)
    _FakeClient.last_headers = {}  # 类属性跨用例残留，逐用例复位
    _FakeClient.response = _FakeResp({})
    return _FakeClient


class TestFetchCodingLimits:
    @pytest.mark.asyncio
    async def test_success_normalizes_limits(self, fake):
        fake.response = _FakeResp({"code": 0, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 100, "usage": 40,
             "remaining": 60, "percentage": 40, "next_reset_time": 1730000000},
            {"type": "TIME_LIMIT", "unit": 5, "number": 10, "usage": 2, "remaining": 8},
            {"not_a_limit": True},
        ]}})
        res = await quota.fetch_coding_limits(_acc())
        assert "error" not in res
        assert len(res["limits"]) == 3  # 非 dict 之外的一律保留（形状由上游决定）
        assert res["limits"][0]["type"] == "TOKENS_LIMIT"
        assert res["limits"][0]["remaining"] == 60

    @pytest.mark.asyncio
    async def test_business_code_becomes_error(self, fake):
        fake.response = _FakeResp({"code": 500, "msg": "当前用户不存在coding plan"})
        res = await quota.fetch_coding_limits(_acc())
        assert res["code"] == 500
        assert "coding plan" in res["error"]

    @pytest.mark.asyncio
    async def test_no_api_key_short_circuits(self, fake):
        res = await quota.fetch_coding_limits(_acc(api_key=""))
        assert "API Key" in res["error"]
        assert fake.last_headers == {}  # 未发起请求

    @pytest.mark.asyncio
    async def test_auth_header_is_bare_api_key(self, fake):
        fake.response = _FakeResp({"code": 0, "data": {"limits": []}})
        await quota.fetch_coding_limits(_acc(api_key="sk-abc"))
        # 上游要求裸值：既不是 Bearer，也不是 JWT
        assert fake.last_headers["authorization"] == "sk-abc"
        assert fake.last_url.endswith("/api/monitor/usage/quota/limit")

    @pytest.mark.asyncio
    async def test_non_json_and_http_error(self, fake):
        fake.response = _FakeResp(ValueError("not json"), status_code=502)
        res = await quota.fetch_coding_limits(_acc())
        assert "非 JSON" in res["error"]

        fake.response = _FakeResp({"data": {}}, status_code=503)
        res = await quota.fetch_coding_limits(_acc())
        assert "503" in res["error"]

    @pytest.mark.asyncio
    async def test_missing_data_yields_empty_limits(self, fake):
        fake.response = _FakeResp({"code": 0})
        res = await quota.fetch_coding_limits(_acc())
        assert res["limits"] == []
