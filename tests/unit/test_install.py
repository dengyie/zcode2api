"""安装初始化仿真测试（2026-09-07）：官方首启序 client/configs → event/report ×2。

单测：请求体形态（对齐 app.asar 逆向的字段集）；集成：走 mock 上游全序 + 失败容错。
"""

from __future__ import annotations

import json
import uuid

import httpx

from app import install


def test_event_body_field_set_matches_official():
    """事件体字段与官方客户端 app.asar 逆向的 sendReport 完全一致。"""
    from app.fingerprint import host_profile

    profile = host_profile()
    body = install._event_body("app_launch", profile, "user-1")
    expected_keys = {
        "event_id", "client_timezone", "client_language", "element_name",
        "event_region", "event_type", "event_text", "event_extra_detail",
        "user_id", "screen_resolution", "app_version", "device_os_category",
        "device_os_version", "device_mid", "mac_id", "marketing_params",
    }
    assert set(body) == expected_keys
    assert body["event_region"] == "app"
    assert body["event_type"] == "view"
    assert body["element_name"] == "app_launch"
    assert body["user_id"] == "user-1"
    uuid.UUID(body["event_id"])  # 合法 UUID
    assert body["device_mid"] == profile.device_mid


async def test_run_install_sequence_against_mock(monkeypatch):
    """走 mock 上游：configs 拉取 + 两事件上报全成功。"""
    captured: list[tuple[str, str, dict | None]] = []

    def transport(handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.path,
                         json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 0, "data": {"configs": {"captcha": {"enabled": True}}}})
        return httpx.Response(200, json={"code": 0, "msg": ""})

    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5))

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is True
    assert result["captcha_enabled"] is True
    assert result["events_reported"] == ["app_launch", "app_daily_active"]
    assert result["errors"] == []
    paths = [p for _, p, _ in captured]
    assert paths[0].endswith("/client/configs")
    assert sum("event/report" in p for p in paths) == 2
    # 事件体字段序（app_launch 在前）
    assert captured[1][2]["element_name"] == "app_launch"
    assert captured[2][2]["element_name"] == "app_daily_active"


async def test_run_install_sequence_tolerates_failures(monkeypatch):
    """上游全挂：结果带 errors，不抛出（安装仿真不影响主服务）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is False
    assert result["events_reported"] == []
    assert len(result["errors"]) == 3  # configs + 2 events


async def test_run_install_sequence_reports_http_error(monkeypatch):
    """非 2xx 事件上报 → 错误文案含 HTTP 状态码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 0, "data": {"configs": {}}})
        return httpx.Response(500, json={"code": -1, "msg": "down"})

    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is True
    assert result["events_reported"] == []
    assert all("HTTP 500" in e for e in result["errors"])
