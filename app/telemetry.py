"""激活遥测上报 —— 官方 event/report 端点的单一事实源。

事件体 16 字段与 app.asar 逆向的 telemetry-core sendReport 逐字段一致
（commit 65a547b 三源实证：首启日志 / asar 逆向 / pxed 实装模仿）。字段集、
URL、业务码判定只在本模块维护：

  - claim.report_activation_events（登录态激活上报，preview 前）
  - install.run_install_sequence（首启安装序）

端点走 settings.ZCODE_EVENT_REPORT_URL（env 可覆盖，测试注入 mock 上游）。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from . import constants, egress, settings

_TIMEOUT = 10
# 瞬态网络重试（2026-11-21 高可用迭代）：激活上报是领取资格信号（app_daily_active
# 先于 preview 的时序铁律），单发即败 = 该号当天资格信号缺失，而白天无领取轮时
# 要拖到 19 点才补（08:11 抖动窗 4 号实锤）。与 billing GET 同口径 3 次 2s/4s
# 阶梯；事件端点无 Authorization、上游按 device_mid+日期去重，重发幂等安全。
_ATTEMPTS = 3
_RETRY_LOG_TAG = "claim"


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
                                timeout: float = _TIMEOUT,
                                proxy: str | None = None) -> None:
    """单条激活事件上报（无 Authorization，官方端点不校验登录态）。

    HTTP >= 400 或业务码非 0 抛 RuntimeError，文案含定位信息（"HTTP 500" /
    "业务码异常"）；httpx.HTTPError 重试 3 次（2s/4s 阶梯）后原样上抛 ——
    容错策略（中止/继续）由调用方定。仅重试传输层瞬态（TransportError），
    HTTP 4xx/5xx 与业务码异常不重试（服务端明确应答 = 重发无意义）。

    出口跟随 settings.BILLING_EGRESS（2026-11-13 起）：激活上报是领取链路的
    资格信号，必须与 billing/claim 同出口（历史 2.7.16 实锤：不同出口 →
    marketing/touch 恒空 → 自动领取全天空转；2026-11-13 tebi 实锤：机房小 ASN
    直连被 billing 风控拒，全链路切家宽代理后 7/7 号领取成功）。
    per-account 出口隔离（2.7.36）：`proxy` 传账号绑定代理时优先。
    """
    if proxy:
        client_kw = {"proxy": proxy, "trust_env": False}
    else:
        trust_env = settings.BILLING_EGRESS == egress.PROXY
        client_kw = {"trust_env": trust_env}
    async with httpx.AsyncClient(timeout=timeout, **client_kw) as client:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                res = await client.post(
                    settings.ZCODE_EVENT_REPORT_URL,
                    # 官方桌面端形态 UA（2026-11-21 claim 链一致性迭代）：
                    # 激活上报是活动投放资格信号，缺省 httpx 默认 UA
                    # （python-httpx/x.y）在 event/report 侧同样自报
                    # 非官方身份——与 build_activation_event_body 的
                    # app_version 字段（BILLING_APP_VERSION）同源对齐。
                    headers={"User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}",
                             "Content-Type": "application/json"},
                    json=build_activation_event_body(element, profile, user_id))
                break
            except httpx.TransportError as err:
                if attempt >= _ATTEMPTS:
                    raise
                wait = 2 * attempt
                from . import logs
                logs.info(_RETRY_LOG_TAG,
                          f"激活上报 {element} 瞬态网络错误（{type(err).__name__}），"
                          f"{wait}s 后重试（{attempt}/{_ATTEMPTS - 1}）")
                await asyncio.sleep(wait)
        assert res is not None  # 循环必经 break/raise
    if res.status_code >= 400:
        raise RuntimeError(f"event/report {element} HTTP {res.status_code}: {res.text[:120]}")
    try:
        body = res.json()
    except ValueError:
        body = None
    code = business_code(body)
    if code != 0:
        raise RuntimeError(f"event/report {element} 业务码异常({code}): {res.text[:120]}")
