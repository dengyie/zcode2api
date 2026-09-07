"""ZCode 首次安装初始化仿真 —— 按官方客户端真实首启顺序请求一遍。

官方桌面端首启实测序（本机 ZCode 首启日志 2026-09-02 + app.asar 逆向
+ pxed AppImage 实装模仿，三源交叉确认）：
  1. GET  /api/v1/client/configs?app_version=…   免鉴权拉取运行配置
     （验证码 scene/region 开关、startPlanPreview 等功能开关）
  2. POST /api/v1/event/report                   激活遥测（无 Authorization）
     element = app_launch → app_daily_active（日活去重由上游按 device_mid+日期算）
  3. （用户登录后）OAuth CLI 流程 → 业务凭证 —— oauth.ZaiAuthFlow 已覆盖；
     登录态的激活上报由 claim.report_activation_events 承担，不在此处

安装语义要点（对齐官方客户端）：
  - client/configs 不带 Authorization（免鉴权端点），带 app_version 查询参数；
    实测带 platform 参数会被拒（3001），故只带 app_version。
  - event/report 的 device_mid + 当日日期构成日活去重键；重复上报同日
    app_daily_active 无副作用（官方客户端每次启动都发）。
  - 全程不需要账号凭证 —— 「安装」先于「登录」，未登录设备同样上报
    app_launch（user_id 为空串），登录后才带 user_id。

对外入口 run_install_sequence()：main.lifespan 每次启动后台执行一次
（官方客户端每次启动都拉 configs + 发 app_launch）。任何失败都不抛出，
errors 列表留痕（安装仿真不影响主服务，与官方「配置拉取失败继续启动」一致）。
"""

from __future__ import annotations

import httpx

from . import constants, logs, telemetry

_CLIENT_TIMEOUT = 15


async def _fetch_client_configs() -> dict:
    """第 1 步：client/configs（免鉴权）。HTTP / 业务码任一失败抛异常。"""
    url = f"{constants.CLIENT_CONFIGS_URL}?app_version={constants.BILLING_APP_VERSION}"
    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        res = await client.get(url, headers={"User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}"})
    res.raise_for_status()
    body = res.json()  # 非 JSON 由调用方按 ValueError 容错
    if telemetry.business_code(body) != 0:
        raise RuntimeError(f"client/configs 业务码异常: {str(body)[:120]}")
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _captcha_enabled(configs: dict) -> bool:
    cfg = configs.get("configs")
    captcha = cfg.get("captcha") if isinstance(cfg, dict) else None
    return bool(captcha.get("enabled")) if isinstance(captcha, dict) else False


async def run_install_sequence() -> dict:
    """按官方首启顺序执行一次安装初始化，返回各步结果（含失败文案）。

    设备身份 = 宿主机真实档案 + 全局持久化 device_mid（quota.device_mid，
    官方语义：一台机器一个 deviceMid）。任何失败都不抛出 —— 调用方
    （main.lifespan）可以完全放心 fire-and-forget。
    """
    result: dict = {"configs_fetched": False, "events_reported": [], "errors": []}

    try:
        from .fingerprint import host_profile
        from .quota import device_mid

        profile = host_profile(device_mid=device_mid())
    except (ValueError, OSError) as err:
        # 宿主机档案不合规且随机兜底也异常（理论上 assign 已兜过一次，防御性留痕）
        logs.err("install", f"安装序设备档案构建失败: {err}")
        result["errors"].append(f"设备档案构建失败: {err}")
        return result

    try:
        configs = await _fetch_client_configs()
        result["configs_fetched"] = True
        result["captcha_enabled"] = _captcha_enabled(configs)
    except (httpx.HTTPError, RuntimeError, ValueError) as err:
        result["errors"].append(f"client/configs 失败: {err}")

    for element in constants.ACTIVATION_ELEMENTS:
        try:
            await telemetry.post_activation_event(profile, "", element,
                                                  timeout=_CLIENT_TIMEOUT)
            result["events_reported"].append(element)
        except (httpx.HTTPError, RuntimeError) as err:
            result["errors"].append(str(err))

    if result["errors"]:
        logs.warn("install", f"安装初始化部分失败: {'; '.join(result['errors'])}")
    else:
        logs.ok("install", f"安装初始化完成（configs={'√' if result['configs_fetched'] else '×'}，"
                           f"events={','.join(result['events_reported']) or '无'}）")
    return result
