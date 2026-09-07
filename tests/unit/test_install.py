"""安装初始化仿真测试（2026-09-07）：官方首启序 client/configs → event/report ×2。

单测：请求体形态（对齐 app.asar 逆向的字段集）；集成：走 mock 上游全序 +
失败容错（HTTP 错误 / 业务码非 0 / 非对象 JSON 都不抛出）。
"""

from __future__ import annotations

import json
import uuid

import httpx

from app import install, telemetry
from app.fingerprint import DeviceProfile


def _profile() -> DeviceProfile:
    return DeviceProfile(
        platform="linux", arch="x64", os_version="6.8.0-45-generic",
        language="zh-CN", timezone="Asia/Shanghai", screen="1920x1080",
        device_mid=str(uuid.uuid4()),
    )


def test_event_body_field_set_matches_official():
    """事件体字段与官方客户端 app.asar 逆向的 sendReport 完全一致。"""
    profile = _profile()
    body = telemetry.build_activation_event_body("app_launch", profile, "user-1")
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


def test_business_code_rejects_non_object_json():
    """合法 JSON 但非对象（null/数组）→ 业务码 -1（视为失败，不抛 AttributeError）。"""
    assert telemetry.business_code(None) == -1
    assert telemetry.business_code([]) == -1
    assert telemetry.business_code("ok") == -1
    assert telemetry.business_code({"code": 0}) == 0
    assert telemetry.business_code({"code": "3001"}) == 3001
    assert telemetry.business_code({}) == -1


def _patch_mock_client(monkeypatch, handler) -> list[tuple[str, str, dict | None]]:
    """httpx.AsyncClient 全局替换为注入 MockTransport 的版本，返回捕获列表。"""
    captured: list[tuple[str, str, dict | None]] = []

    def handler_wrapped(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.path,
                         json.loads(request.content) if request.content else None))
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handler_wrapped),
                                 timeout=kwargs.get("timeout", 5))

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    return captured


async def test_run_install_sequence_against_mock(monkeypatch):
    """走 mock 上游：configs 拉取 + 两事件上报全成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 0, "data": {"configs": {"captcha": {"enabled": True}}}})
        return httpx.Response(200, json={"code": 0, "msg": ""})

    captured = _patch_mock_client(monkeypatch, handler)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is True
    assert result["captcha_enabled"] is True
    assert result["events_reported"] == ["app_launch", "app_daily_active"]
    assert result["errors"] == []
    paths = [p for _, p, _ in captured]
    assert paths[0].endswith("/client/configs")
    assert sum("event/report" in p for p in paths) == 2
    # 事件体字段序（app_launch 在前）+ 安装态 user_id 为空串
    assert captured[1][2]["element_name"] == "app_launch"
    assert captured[2][2]["element_name"] == "app_daily_active"
    assert captured[1][2]["user_id"] == ""


async def test_run_install_sequence_tolerates_failures(monkeypatch):
    """上游全挂：结果带 errors，不抛出（安装仿真不影响主服务）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_mock_client(monkeypatch, handler)
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

    _patch_mock_client(monkeypatch, handler)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is True
    assert result["events_reported"] == []
    assert all("HTTP 500" in e for e in result["errors"])


async def test_run_install_sequence_configs_business_code(monkeypatch):
    """configs HTTP 200 但业务码非 0（如 3001）→ 计入 errors，不算成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 3001, "msg": "bad param"})
        return httpx.Response(200, json={"code": 0})

    _patch_mock_client(monkeypatch, handler)
    result = await install.run_install_sequence()
    assert result["configs_fetched"] is False
    assert result["events_reported"] == ["app_launch", "app_daily_active"]
    assert any("client/configs 失败" in e and "3001" in e for e in result["errors"])


async def test_run_install_sequence_event_json_null(monkeypatch):
    """event/report 返回合法 JSON null（代理/WAF 形态）→ 业务码失败，不抛 AttributeError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, content=b"null")

    _patch_mock_client(monkeypatch, handler)
    result = await install.run_install_sequence()
    assert result["events_reported"] == []
    assert all("业务码异常" in e for e in result["errors"])
