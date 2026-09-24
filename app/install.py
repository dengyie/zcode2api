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

import time

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


async def run_install_sequence_for_account(account) -> dict:
    """按账号安装序：client/configs + 激活事件，绑定该账号的安装身份。

    与 run_install_sequence 的差别：
      - 设备档案 = 账号自身指纹（profile_for → 每账号独立 device_mid，
        官方语义「每台安装一台设备」），而非全局 device_mid；
      - user_id 从账号 JWT 解出（登录态安装）；无 JWT（apiKey 账号）退回空串
        —— 官方未登录安装形态；
      - 幂等：account.installed_at 非空直接跳过（skipped=True，零上游请求），
        完成后落 installed_at 并持久化。

    任何失败都不抛出，errors 留痕（同 run_install_sequence 约定）。
    """
    from .claim import jwt_user_id
    from .fingerprint import profile_for
    from .store import store

    result: dict = {"configs_fetched": False, "events_reported": [],
                    "errors": [], "installed": False, "skipped": False}
    if account.installed_at:
        result["skipped"] = True
        return result

    profile = profile_for(account)
    user_id = jwt_user_id(account) or ""

    try:
        await _fetch_client_configs()
        result["configs_fetched"] = True
    except (httpx.HTTPError, RuntimeError, ValueError) as err:
        result["errors"].append(f"client/configs 失败: {err}")

    for element in constants.ACTIVATION_ELEMENTS:
        try:
            await telemetry.post_activation_event(profile, user_id, element,
                                                  timeout=_CLIENT_TIMEOUT)
            result["events_reported"].append(element)
        except (httpx.HTTPError, RuntimeError) as err:
            result["errors"].append(str(err))

    if not result["errors"]:
        live = store.find(account.provider, account.id)
        if live is None:
            result["errors"].append("账号已删除，跳过安装落库")
            logs.warn("install", f"账号 {account.name} 安装序完成前已被删除")
            return result
        live.installed_at = time.time()
        store.update_account(live)
        account.installed_at = live.installed_at
        result["installed"] = True
        logs.ok("install", f"账号 {account.name} 安装序完成（install_id={account.install_id}）")
    else:
        logs.warn("install", f"账号 {account.name} 安装序部分失败: {'; '.join(result['errors'])}")
    return result


async def run_install_sequence() -> dict:
    """按官方首启顺序执行一次安装初始化，返回各步结果（含失败文案）。

    设备身份 = 官方桌面常量档案 + 全局持久化 device_mid（quota.device_mid）。
    进程级安装序只拉 configs / 报日活，不得把部署机云内核上报成用户设备；
    每账号安装序才使用该号自己的生成 SKU。任何失败都不抛出。
    """
    from .fingerprint import DeviceProfile
    from .quota import device_mid

    result: dict = {"configs_fetched": False, "events_reported": [], "errors": []}
    plat, _, arch = constants.CLIENT_PLATFORM.partition("-")
    profile = DeviceProfile(
        platform=plat or "darwin",
        arch=arch or "arm64",
        os_version=constants.IDENTITY_OS_VERSION,
        language=constants.IDENTITY_CLIENT_LANGUAGE,
        timezone=constants.IDENTITY_CLIENT_TIMEZONE,
        screen=constants.ACTIVATION_SCREEN_RESOLUTION,
        device_mid=device_mid(),
    )

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
