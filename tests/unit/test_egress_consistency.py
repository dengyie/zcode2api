"""出口一致性：所有「官方首启模拟链路」的 HTTP 客户端必须跟随同一开关。

2026-11-13 线上定性：billing/claim/telemetry 走家宽代理（BILLING_EGRESS=proxy），
而 quota 刷新 / install 首启 / 手动领取验证码配置曾各自硬编码 trust_env，
出口分裂本身就是上游「unusual activity」(3012) 的指纹矛盾信号（2.7.33 收敛）。
"""

from __future__ import annotations

import pytest

from app import egress, settings


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """记录构造参数（trust_env）的 httpx.AsyncClient 替身。"""

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        # 记录在基类上：子类（_bind 生成的 Bound）实例化时也要能读到
        _FakeClient.last_kwargs = kwargs
        self._payload = kwargs.pop("_payload", {"code": 0, "data": {}})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, headers=None, **_kw):
        return _Resp(self._payload)


def _bind(client_cls, payload):
    class Bound(_FakeClient):
        def __init__(self, *args, **kwargs):
            kwargs["_payload"] = payload
            super().__init__(*args, **kwargs)

    return Bound


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("proxy", True), ("direct", False)])
async def test_install_client_configs_follows_billing_egress(monkeypatch, mode, expected):
    from app import install

    monkeypatch.setattr(settings, "BILLING_EGRESS", mode)
    monkeypatch.setattr(install.httpx, "AsyncClient", _bind(_FakeClient, {"code": 0, "data": {}}))
    _FakeClient.last_kwargs = {}

    await install._fetch_client_configs()

    assert _FakeClient.last_kwargs.get("trust_env") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("proxy", True), ("direct", False)])
async def test_quota_fetch_follows_billing_egress(fresh_app, monkeypatch, mode, expected):
    from app import quota

    acc = fresh_app.add_account("zai", "egress-q", "h7.eyJzdWIiOiJxIn0.sig")
    monkeypatch.setattr(settings, "BILLING_EGRESS", mode)
    monkeypatch.setattr(
        quota.httpx, "AsyncClient",
        _bind(_FakeClient, {"code": 0, "data": {"plans": [], "balances": []}}),
    )
    _FakeClient.last_kwargs = {}

    await quota.fetch_quota(acc)

    assert _FakeClient.last_kwargs.get("trust_env") is expected


@pytest.mark.asyncio
async def test_manual_claim_captcha_config_uses_billing_egress(monkeypatch):
    """手动领取的验证码配置必须按 BILLING_EGRESS 出口拉取（token 出口一致）。"""
    from app.captcha import captcha_manager
    from app.routes import admin_api

    seen: list[str | None] = []

    async def fake_fetch(tag=None):
        seen.append(tag)
        return {"enabled": True, "sceneId": "s", "region": "r", "prefix": "p"}

    monkeypatch.setattr(settings, "BILLING_EGRESS", "proxy")
    monkeypatch.setattr(captcha_manager, "fetch_config", fake_fetch)

    body = await admin_api.claim_captcha_config()
    assert seen == ["proxy"]
    assert body["scene_id"] == "s"


def test_egress_constants_sane():
    assert {egress.DIRECT, egress.PROXY} == {"direct", "proxy"}
