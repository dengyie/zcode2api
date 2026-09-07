"""ZCode 首次安装初始化仿真 —— 按官方客户端真实首启顺序请求一遍。

官方桌面端首启实测序（本机 ZCode 3.10.2 首启日志 2026-09-02 + zapi 镜像）：
  1. GET  /api/v1/client/configs?app_version=…   免鉴权拉取运行配置
     （验证码 scene/region 开关、startPlanPreview 等功能开关）
  2. POST /api/v1/event/report                   激活遥测（无 Authorization）
     element = app_launch → app_daily_active（日活去重由上游按 device_mid+日期算）
  3. （用户登录后）OAuth CLI 流程 → 业务凭证 —— oauth.ZaiAuthFlow 已覆盖，不在此处

安装语义要点（对齐官方客户端）：
  - client/configs 不带 Authorization（免鉴权端点），带 app_version 查询参数；
    实测带 platform 参数会被拒（3001），故只带 app_version。
  - event/report 的 device_mid + 当日日期构成日活去重键；重复上报同日
    app_daily_active 无副作用（官方客户端每次启动都发）。
  - 全程不需要账号凭证 —— 「安装」先于「登录」，未登录设备同样上报
    app_launch（user_id 为空串），登录后才带 user_id。

对外入口：
  run_install_sequence(account=None) — 一次完整安装初始化；account 给定时
  用其指纹档案（每账号设备）与 JWT user_id，否则用全局 device_mid。
"""

from __future__ import annotations

import uuid

import httpx

from . import constants, logs

_CLIENT_TIMEOUT = 15


def _event_body(element: str, profile, user_id: str) -> dict:
    """激活事件体（与 claim.report_activation_events 同形，单一事实源在此复用）。"""
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


async def _fetch_client_configs() -> dict:
    """第 1 步：client/configs（免鉴权）。返回 data 或 {}（失败不阻断安装序）。"""
    url = f"{constants.CLIENT_CONFIGS_URL}?app_version={constants.BILLING_APP_VERSION}"
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        res = await client.get(url, headers={"User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}"})
    res.raise_for_status()
    return res.json().get("data") or {}


async def _report_event(profile, user_id: str, element: str) -> None:
    """第 2 步：单条激活事件上报。非 2xx 或业务码非 0 抛 RuntimeError。"""
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        res = await client.post(constants.EVENT_REPORT_URL,
                                headers={"Content-Type": "application/json"},
                                json=_event_body(element, profile, user_id))
    if res.status_code >= 400:
        raise RuntimeError(f"event/report {element} HTTP {res.status_code}: {res.text[:120]}")
    try:
        code = int(res.json().get("code", -1))
    except ValueError:
        code = -1
    if code != 0:
        raise RuntimeError(f"event/report {element} 业务码异常: {res.text[:120]}")


async def run_install_sequence(account=None) -> dict:
    """按官方首启顺序执行一次安装初始化，返回各步结果（含失败文案）。

    account 给定：用账号指纹 + JWT user_id（登录态安装补跑）；
    account 为空：宿主机真实档案 + 全局 device_mid（纯安装态，user_id 空串）。
    任一步失败不抛出 —— 返回结构里带 errors 列表供上层留痕（安装仿真失败
    不应影响主服务，与官方客户端「配置拉取失败继续启动」语义一致）。
    """
    if account is not None:
        from .claim import jwt_user_id
        from .fingerprint import profile_for

        profile = profile_for(account)
        user_id = jwt_user_id(account) or ""
    else:
        from .fingerprint import host_profile
        from .quota import device_mid

        profile = host_profile(device_mid=device_mid())
        user_id = ""

    result: dict = {"configs_fetched": False, "events_reported": [], "errors": []}

    try:
        configs = await _fetch_client_configs()
        result["configs_fetched"] = True
        result["captcha_enabled"] = bool((configs.get("configs") or {}).get("captcha", {}).get("enabled"))
    except (httpx.HTTPError, ValueError) as err:
        result["errors"].append(f"client/configs 失败: {err}")

    for element in constants.ACTIVATION_ELEMENTS:
        try:
            await _report_event(profile, user_id, element)
            result["events_reported"].append(element)
        except (httpx.HTTPError, RuntimeError, ValueError) as err:
            result["errors"].append(str(err))

    if result["errors"]:
        logs.warn("install", f"安装初始化部分失败: {'; '.join(result['errors'])}")
    else:
        logs.ok("install", f"安装初始化完成（configs={'√' if result['configs_fetched'] else '×'}，"
                           f"events={','.join(result['events_reported']) or '无'}）")
    return result
