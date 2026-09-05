"""套餐领取（Z.AI billing/preview + billing/claim）。

链路与 zcode-switch claim.rs 同形：
  1. GET  {BILLING_BASE}/billing/preview?app_version=&platform= → data.plans[]
  2. 领取需阿里云无痕验证码：CaptchaManager 服务端求解 → X-Aliyun-Captcha-Verify-Param
  3. POST {BILLING_BASE}/billing/claim  body {"plan_id":...}（+ 可选 Verify-Region 头）

上游业务码语义（沿用 zcode-switch 映射）：1001 套餐不存在 / 1002 活动结束 /
1003 已领取过 / 1004 不符合条件 / 1005 今日名额用完 / 3001 参数错误 /
3007 验证码失败（换验证码重试一次）/ 401 未登录。
"""

from __future__ import annotations

import base64
import json
import uuid

import httpx

from . import constants, logs, settings
from .captcha import captcha_manager
from .models import Account


class ClaimError(Exception):
    """业务失败（含上游 code 语义），message 面向用户。"""


_CLAIM_FAIL = {
    1001: "套餐不存在",
    1002: "活动已结束或套餐暂不可领取",
    1003: "该套餐已经领取过",
    1004: "不符合领取条件",
    1005: "今日领取名额已用完",
    3001: "领取参数错误，请刷新后重试",
    3007: "验证码校验失败，请重试",
    401: "请先登录后再领取",
}


def _fail_message(code: int, body: dict) -> str:
    base = _CLAIM_FAIL.get(code, "领取失败")
    server = body.get("msg") or body.get("message") or ""
    return f"{base}（{server}）" if server else base


def _business_code(body: dict) -> int:
    code = body.get("code")
    try:
        return int(code) if code is not None else -1
    except (TypeError, ValueError):
        return -1


def parse_plan(raw: dict) -> dict | None:
    """提取可领取套餐（plan_id/name/描述/优先级 + model_usage token 授权项）。"""
    plan_id = str(raw.get("plan_id") or raw.get("planId") or "").strip()
    if not plan_id:
        return None
    grants = []
    for ent in raw.get("entitlements") or []:
        if ent.get("meter") != "model_usage" or ent.get("unit_type") != "token":
            continue
        name = str(ent.get("show_name") or ent.get("showName") or "").strip()
        if not name:
            continue
        units = ent.get("grant_units", ent.get("grantUnits")) or 0
        grants.append({
            "name": name,
            "units": float(units),
            "period": ent.get("period") or "one_time",
        })
    return {
        "plan_id": plan_id,
        "name": str(raw.get("name") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "priority": raw.get("priority") or 0,
        "grants": grants,
    }


async def _billing_request(account: Account, method: str, path: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers"))
    async with httpx.AsyncClient(timeout=25) as client:
        res = await client.request(
            method, f"{settings.ZCODE_BILLING_BASE}{path}",
            headers=headers, **kwargs,
        )
    if res.status_code in (401, 403):
        text = (res.text or "").lower()
        if "captcha" not in text and "verify" not in text:
            raise ClaimError(f"鉴权失败 HTTP {res.status_code}")
    try:
        body = res.json()
    except ValueError:
        raise ClaimError(f"上游响应非 JSON HTTP {res.status_code}") from None
    return body


def jwt_user_id(account: Account) -> str | None:
    """JWT payload 的 user_id（zcode-switch telemetry_user_id 同源语义）。

    官方客户端事件上报以 user_id 标识用户；hub 不存 user_info，直接从 JWT
    解出（user_id 优先，sub 兜底，两者同为 36 位 uuid）。
    """
    token = (account.jwt_token or "").strip()
    if not token:
        return None
    try:
        seg = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError):
        return None
    uid = payload.get("user_id") or payload.get("sub")
    if not isinstance(uid, str) or not uid.strip():
        return None
    return uid.strip()


async def report_activation_events(account: Account) -> str | None:
    """上报官方客户端激活事件（app_launch + app_daily_active），返回错误或 None。

    zcode-switch claim_refresh 同形：preview 前模拟桌面端当日活跃（疑似活动
    套餐投放资格信号）。请求无 Authorization（上游事件端点不校验）；任何失败
    仅返回文案，不阻断 preview。
    """
    from .quota import device_mid

    user_id = jwt_user_id(account)
    if not user_id:
        return "JWT 无 user_id，跳过激活上报"
    headers = {"Content-Type": "application/json"}
    for element in constants.ACTIVATION_ELEMENTS:
        body = {
            "event_id": str(uuid.uuid4()),
            "client_timezone": constants.IDENTITY_CLIENT_TIMEZONE,
            "client_language": constants.IDENTITY_CLIENT_LANGUAGE,
            "element_name": element,
            "event_region": "app",
            "event_type": "view",
            "event_text": "",
            "event_extra_detail": {},
            "user_id": user_id,
            "screen_resolution": constants.ACTIVATION_SCREEN_RESOLUTION,
            "app_version": constants.BILLING_APP_VERSION,
            "device_os_category": constants.IDENTITY_OS_CATEGORY,
            "device_os_version": constants.IDENTITY_OS_VERSION,
            "device_mid": device_mid(),
            "mac_id": "",
            "marketing_params": "{}",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(settings.ZCODE_EVENT_REPORT_URL,
                                        headers=headers, json=body)
        except httpx.HTTPError as err:
            return f"激活事件 {element} 请求失败: {err}"
        if res.status_code >= 400:
            return f"激活事件 {element} HTTP {res.status_code}"
        try:
            if int(res.json().get("code", -1)) != 0:
                return f"激活事件 {element} 上游拒绝: {res.text[:120]}"
        except ValueError:
            return f"激活事件 {element} 响应非 JSON"
    return None


async def preview_plans(account: Account) -> list[dict]:
    """拉取账号当前可领取套餐，按优先级降序。"""
    from .quota import _auth_headers

    body = await _billing_request(
        account, "GET", "/billing/preview",
        headers=_auth_headers(account),
        # 官方客户端 preview 用 TH()（darwin-arm64 等）；platform 参数上游宽容。
        # 实测 client/configs 才拒 platform 参数。
        params={"app_version": constants.BILLING_APP_VERSION,
                "platform": constants.CLIENT_PLATFORM},
    )
    code = _business_code(body)
    if code != 0:
        raise ClaimError(_fail_message(code, body))
    raw_plans = (body.get("data") or {}).get("plans") or []
    plans = [parsed for parsed in (parse_plan(p) for p in raw_plans) if parsed]
    plans.sort(key=lambda p: (-p["priority"], p["plan_id"]))
    return plans


async def _auto_pick_plan(account: Account, plan_id: str | None) -> tuple[str, str, list]:
    """plan_id 为空时 preview 自动选优先级最高套餐。返回 (plan_id, plan_name, grants)。"""
    if plan_id:
        return plan_id, "", []
    plans = await preview_plans(account)
    if not plans:
        raise ClaimError("没有待领取的套餐")
    best = plans[0]
    return best["plan_id"], best["name"] or best["plan_id"], best["grants"]


def _claim_headers(account: Account, verify_param: str, region: str | None) -> dict:
    """billing/claim 客户端请求头形态（asar claimManualPlan）。

    实测缺版本/平台头时即使验证码有效也 3007；X-Device-Mid 由 _auth_headers 提供。
    """
    from .quota import _auth_headers

    headers = _auth_headers(account)
    headers[constants.CAPTCHA_HEADER] = verify_param
    if region and region.strip():
        headers["X-Aliyun-Captcha-Verify-Region"] = region.strip()
    # 实测缺版本/平台头时即使验证码有效也 3007（_auth_headers 已带，此处显式
    # 兜底防止基座头漂移）
    headers["X-ZCode-App-Version"] = constants.BILLING_APP_VERSION
    headers["X-Platform"] = constants.X_PLATFORM
    return headers


async def _post_claim(account: Account, headers: dict, plan_id: str) -> dict:
    """提交 billing/claim 并翻译业务码。"""
    body = await _billing_request(
        account, "POST", "/billing/claim",
        headers=headers, json={"plan_id": plan_id},
    )
    code = _business_code(body)
    if code != 0:
        raise ClaimError(_fail_message(code, body))
    return body


async def claim_with_captcha(
    account: Account,
    verify_param: str,
    region: str | None,
    plan_id: str | None = None,
) -> dict:
    """手动领取：verify_param 由用户浏览器内阿里 SDK 滑块产生，本端只做转发。

    plan_id 缺省时先 preview 自动选优先级最高套餐（无需验证码）。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        raise ClaimError("仅 Coding Plan (JWT) 账号支持领取")
    if not (verify_param or "").strip():
        raise ClaimError("缺少验证码参数，请先完成人机验证")

    plan_id, plan_name, grants = await _auto_pick_plan(account, plan_id or None)
    headers = _claim_headers(account, verify_param.strip(), region)
    await _post_claim(account, headers, plan_id)
    return {"plan_id": plan_id, "plan_name": plan_name, "grants": grants}


async def claim(account: Account, plan_id: str | None = None) -> dict:
    """领取套餐。plan_id 缺省时自动选优先级最高的可领套餐。

    返回 {"plan_id", "plan_name", "grants"}；3007（验证码失败）自动换码重试一次。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        raise ClaimError("仅 Coding Plan (JWT) 账号支持领取")

    plan_id, plan_name, grants = await _auto_pick_plan(account, plan_id)
    last_err: ClaimError | None = None
    for attempt in (1, 2):
        verify_param, verify_region = await captcha_manager.get_verify_param()
        config = await captcha_manager.fetch_config()
        headers = _claim_headers(account, verify_param, verify_region or config.get("region"))

        body = await _billing_request(
            account, "POST", "/billing/claim",
            headers=headers, json={"plan_id": plan_id},
        )
        code = _business_code(body)
        if code == 0:
            return {"plan_id": plan_id, "plan_name": plan_name, "grants": grants}
        if code == 3007 and attempt == 1:
            logs.warn("claim", f"账号 {account.name} 验证码被拒，换码重试")
            captcha_manager.invalidate()
            last_err = ClaimError(_fail_message(code, body))
            continue
        raise ClaimError(_fail_message(code, body))
    raise last_err or ClaimError("领取失败")
