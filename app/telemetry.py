"""激活遥测上报 —— 官方 event/report 端点的单一事实源。

事件体 16 字段与 app.asar 逆向的 telemetry-core sendReport 逐字段一致
（commit 65a547b 三源实证：首启日志 / asar 逆向 / pxed 实装模仿）。字段集、
URL、业务码判定只在本模块维护：

  - claim.report_activation_events（登录态激活上报，preview 前）
  - install.run_install_sequence（首启安装序）

端点走 settings.ZCODE_EVENT_REPORT_URL（env 可覆盖，测试注入 mock 上游）。
"""

from __future__ import annotations

import uuid

import httpx

from . import constants, settings

_TIMEOUT = 10


def build_activation_event_body(element: str, profile, user_id: str) -> dict:
    """激活事件体（官方 sendReport 字段集固定为这 16 个）。"""
    return {
        "event_id": str(uuid.uuid4()),
        "client_timezone": profile.timezone,
        "client_language": profile.language,
        "element_name": element,
        "event_region": "app",
        "event_type": "view",
        "event_text": "",
        "event_extra_detail": {},
        "user_id": user_id,
        "screen_resolution": profile.screen,
        "app_version": constants.BILLING_APP_VERSION,
        "device_os_category": profile.os_category,
        "device_os_version": profile.os_version,
        "device_mid": profile.device_mid,
        "mac_id": "",
        "marketing_params": "{}",
    }


def business_code(body) -> int:
    """上游业务码；非对象 JSON / 缺 code / 非数字 → -1（视为失败）。"""
    if not isinstance(body, dict):
        return -1
    try:
        return int(body.get("code", -1))
    except (TypeError, ValueError):
        return -1


async def post_activation_event(profile, user_id: str, element: str,
                                timeout: float = _TIMEOUT) -> None:
    """单条激活事件上报（无 Authorization，官方端点不校验登录态）。

    HTTP >= 400 或业务码非 0 抛 RuntimeError，文案含定位信息（"HTTP 500" /
    "业务码异常"）；httpx.HTTPError 原样上抛 —— 容错策略（中止/继续）由调用方定。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(settings.ZCODE_EVENT_REPORT_URL,
                                headers={"Content-Type": "application/json"},
                                json=build_activation_event_body(element, profile, user_id))
    if res.status_code >= 400:
        raise RuntimeError(f"event/report {element} HTTP {res.status_code}: {res.text[:120]}")
    try:
        body = res.json()
    except ValueError:
        body = None
    code = business_code(body)
    if code != 0:
        raise RuntimeError(f"event/report {element} 业务码异常({code}): {res.text[:120]}")
