"""claim 链版本与指纹一致性守护（2026-11-21 迭代三）。

取证（production-code-quality-review 视角）：
- billing 族（preview/claim/touch/quota/install/configs/event）版本身份 =
  BILLING_APP_VERSION（version_tracker 无正文锁自动跟随官方最新）；
- messages 链 = CLIENT_APP_VERSION（正文锁 3.17.2，刻意落后）。
  两链端点/头必须各锚各版，混用即指纹矛盾（同端点双版本、缺 UA 自报
  python-httpx 等）。

本文件锁定三处收口点，任何一处回退都会在此失败：
  1. captcha.fetch_config（client/configs）——claim 链的验证码配置源，
     查询串与 UA 必须锚 billing 版（install 同端点路径已是 billing 版，
     原实现用 messages 钉版 query 且不带 UA = httpx 默认 UA 自报身份）。
  2. telemetry.post_activation_event（event/report）——活动投放资格信号，
     必须带 ZCode/<BILLING_APP_VERSION> UA（事件体 app_version 字段同源）。
  3. version_tracker.fetch_remote_manifest（electron manifest）——官方客户端
     查更新上报自己的当前版本 = 跟随后最新 billing 版。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app import constants, settings


def _make_jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h1.{seg}.sig"


def _capture_client(monkeypatch, handler):
    """把 httpx.AsyncClient 换成捕获请求的 mock（test_install.py 同款手法）。"""
    captured: list[tuple[str, str, dict, bytes | None]] = []

    class _Resp:
        def __init__(self, req: httpx.Request):
            self.status_code = 200
            self._req = req

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"configs": {"captcha": {
                "enabled": True, "prefix": "mockpre", "region": "cn",
                "sceneId": "mock-scene",
            }}}}

        @property
        def text(self):
            return ""

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            req = httpx.Request("GET", url, headers=kw.get("headers") or {})
            captured.append(("GET", str(req.url), dict(req.headers), None))
            return _Resp(req)

        async def post(self, url, **kw):
            req = httpx.Request("POST", url, headers=kw.get("headers") or {},
                                content=kw.get("json") and json.dumps(kw["json"]).encode())
            captured.append(("POST", str(req.url), dict(req.headers), kw.get("json")))
            return _Resp(req)

        async def request(self, method, url, **kw):
            req = httpx.Request(method, url, headers=kw.get("headers") or {})
            captured.append((method, str(req.url), dict(req.headers), None))
            return _Resp(req)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return captured


class TestCaptchaFetchConfigIdentity:
    @pytest.mark.asyncio
    async def test_configs_query_and_ua_anchored_to_billing_version(self, monkeypatch):
        """client/configs（captcha 配置路径）查询串与 UA 均锚 billing 版。"""
        from app.captcha import captcha_manager

        # 清缓存：全量套件里前序测试（test_egress.py 等）会写 _config_cache，
        # 不清则命中缓存直接 return、captured 为空（单跑不暴露）。
        monkeypatch.setattr(captcha_manager, "_config_cache", {})
        monkeypatch.setattr(captcha_manager, "_config_cache_at", {})
        captured = _capture_client(monkeypatch, None)
        await captcha_manager.fetch_config(egress_tag="direct", proxy="http://127.0.0.1:9")
        method, url, headers, _ = captured[0]
        assert "client/configs" in url
        assert f"app_version={constants.BILLING_APP_VERSION}" in url
        # messages 钉版（3.17.2）若与 billing 版相同则此断言天然通过；
        # 分裂场景（跟随后的真实线上态）由「≠ messages query」语义覆盖
        assert headers.get("user-agent") == f"ZCode/{constants.BILLING_APP_VERSION}"

    @pytest.mark.asyncio
    async def test_configs_query_follows_runtime_version_bump(self, monkeypatch):
        """version_tracker 运行期重赋 BILLING_APP_VERSION 后，下一次拉取立即跟随。"""
        from app.captcha import captcha_manager

        monkeypatch.setattr(captcha_manager, "_config_cache", {})
        monkeypatch.setattr(captcha_manager, "_config_cache_at", {})
        monkeypatch.setattr(constants, "BILLING_APP_VERSION", "3.19.9")
        captured = _capture_client(monkeypatch, None)
        await captcha_manager.fetch_config(egress_tag="direct", proxy="http://127.0.0.1:9")
        assert "app_version=3.19.9" in captured[0][1]
        assert captured[0][2].get("user-agent") == "ZCode/3.19.9"


class TestTelemetryIdentity:
    @pytest.mark.asyncio
    async def test_event_report_ua_official_shape(self, monkeypatch):
        """event/report（激活上报，领取资格信号）必须带 ZCode/<billing 版> UA。"""
        from app import telemetry
        from app.fingerprint import random_profile

        captured = _capture_client(monkeypatch, None)
        await telemetry.post_activation_event(random_profile(), "u-1", "app_launch",
                                              proxy="http://127.0.0.1:9")
        method, url, headers, body = captured[0]
        assert "event/report" in url
        assert headers.get("user-agent") == f"ZCode/{constants.BILLING_APP_VERSION}"
        assert body["app_version"] == constants.BILLING_APP_VERSION  # 与 UA 同源

    @pytest.mark.asyncio
    async def test_event_report_ua_follows_runtime_version_bump(self, monkeypatch):
        from app import telemetry
        from app.fingerprint import random_profile

        monkeypatch.setattr(constants, "BILLING_APP_VERSION", "3.20.1")
        captured = _capture_client(monkeypatch, None)
        await telemetry.post_activation_event(random_profile(), "u-1", "app_daily_active",
                                              proxy="http://127.0.0.1:9")
        assert captured[0][2].get("user-agent") == "ZCode/3.20.1"


class TestManifestIdentity:
    @pytest.mark.asyncio
    async def test_manifest_ua_anchored_to_billing_version(self, monkeypatch):
        """electron manifest 查更新的 UA = 官方客户端当前版本（跟随后 billing 版）。"""
        from app.version_tracker import version_tracker

        monkeypatch.setattr(settings, "ZCODE_MANIFEST_URL",
                            constants.ELECTRON_MANIFEST_URL)
        captured = _capture_client(monkeypatch, None)
        await version_tracker.fetch_remote_manifest()
        method, url, headers, _ = captured[0]
        assert "electron/manifest" in url
        assert headers.get("user-agent") == f"ZCode/{constants.BILLING_APP_VERSION}"

    @pytest.mark.asyncio
    async def test_manifest_ua_follows_runtime_version_bump(self, monkeypatch):
        from app.version_tracker import version_tracker

        monkeypatch.setattr(settings, "ZCODE_MANIFEST_URL",
                            constants.ELECTRON_MANIFEST_URL)
        monkeypatch.setattr(constants, "BILLING_APP_VERSION", "3.21.0")
        captured = _capture_client(monkeypatch, None)
        await version_tracker.fetch_remote_manifest()
        assert captured[0][2].get("user-agent") == "ZCode/3.21.0"


class TestNoBlockOnClaimPath:
    @pytest.mark.asyncio
    async def test_fetch_config_failure_returns_fallback_not_raise(self, monkeypatch):
        """一致性收紧不得阻塞 claim 主链：configs 拉取失败仍走兜底路径返回。"""
        from app.captcha import captcha_manager

        class _FailClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kw):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "AsyncClient", _FailClient)
        monkeypatch.setattr(captcha_manager, "_config_cache", {})
        monkeypatch.setattr(captcha_manager, "_config_cache_at", {})
        # 不带 proxy（走跨出口兜底/defaults/stale 路径），任何结果都不允许抛出
        cfg = await captcha_manager.fetch_config(egress_tag="direct")
        assert isinstance(cfg, dict)
