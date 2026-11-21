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

import asyncio
import base64
import json
import time

import httpx

from . import constants, egress, logs, settings
from .captcha import CaptchaSolveError, captcha_manager
from .models import Account, Status


class ClaimError(Exception):
    """业务失败（含上游 code 语义），message 面向用户。

    next_at 仅 1005（名额用完）携带：上游 data.plan.ends_at（秒 → 毫秒），
    即名额恢复时间（zcode-switch claim.rs claim_error 同形）。
    """

    def __init__(self, message: str, *, code: int = -1, next_at: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_at = next_at


AUTH_EXPIRED_MESSAGE = "凭证失效，请重新授权"

_CLAIM_FAIL = {
    1001: "套餐不存在",
    1002: "活动已结束或套餐暂不可领取",
    1003: "该套餐已经领取过",
    1004: "不符合领取条件",
    1005: "今日领取名额已用完",
    3001: "领取参数错误，请刷新后重试",
    3002: "请求过于频繁，等待下轮领取窗口",
    3007: "验证码校验失败，请重试",
    401: "请先登录后再领取",
}


def billing_block_reason(account: Account, *, action: str = "领取") -> str | None:
    """JWT 不可打 billing 时的用户文案；可打则返回 None。

    只拦「JWT 本身不可用」：非 Coding Plan 账号 / 无 token / INVALID。
    **冷却、风控 DISABLED、人工停用都不拦**——领取与使用是两条链，
    账号停着也照样要能把投放领进账（2026-11-04 用户口径）。
    """
    if account.mode != "jwt" or not (account.jwt_token or "").strip():
        return f"非 Coding Plan 账号，已跳过{action}"
    if account.status == Status.INVALID:
        return AUTH_EXPIRED_MESSAGE
    return None


def _mark_auth_failure(account: Account) -> None:
    from .store import store

    live = store.find(account.provider, account.id)
    if live is None:
        return
    live.status = Status.INVALID
    live.last_error = AUTH_EXPIRED_MESSAGE
    store.update_account(live)
    account.status = live.status
    account.last_error = live.last_error


def _fail_error(code: int, body: dict) -> ClaimError:
    """业务码 → ClaimError；1005 附带名额恢复时间（data.plan.ends_at 秒 → 毫秒）。"""
    base = _CLAIM_FAIL.get(code, "领取失败")
    server = body.get("msg") or body.get("message") or ""
    message = f"{base}（{server}）" if server else base
    next_at = None
    if code == 1005:
        ends = ((body.get("data") or {}).get("plan") or {}).get("ends_at")
        if isinstance(ends, (int, float)) and ends > 0:
            next_at = int(ends * 1000)
    return ClaimError(message, code=code, next_at=next_at)


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
        if not isinstance(ent, dict):
            continue
        if ent.get("meter") != "model_usage" or ent.get("unit_type") != "token":
            continue
        name = str(ent.get("show_name") or ent.get("showName") or "").strip()
        if not name:
            continue
        units = ent.get("grant_units", ent.get("grantUnits")) or 0
        try:
            units = float(units)
        except (TypeError, ValueError):
            units = 0.0  # 上游偶发字符串/None 形态：不让单个脏字段炸掉整个 preview
        grants.append({
            "name": name,
            "units": units,
            "period": ent.get("period") or "one_time",
        })
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0  # preview 排序要求可比较，脏值归零而不是抛 TypeError
    return {
        "plan_id": plan_id,
        "name": str(raw.get("name") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "priority": priority,
        "grants": grants,
    }


def extract_plans_from_marketing_deliveries(deliveries: list) -> list[dict]:
    """从 marketing/touch 的 deliveries 载荷中提取待领取套餐。

    官方客户端结构实证（2026-11-03 逆向 + 线上实测）：
    delivery.banner.buttons[].action: {"type": "claim_zcode_plan", "args": {"plan_id": "..."}}
    delivery.banner.args.zcode_plan: {"plan_id": "...", "name": "...", "priority": ..., "entitlements": [...]}
    """
    plans: list[dict] = []
    seen_ids: set[str] = set()

    def _scan(obj: object) -> None:
        if isinstance(obj, dict):
            # 1. 优先提取挂在 args 里的完整 zcode_plan
            zp = obj.get("zcode_plan") or obj.get("zcodePlan")
            if isinstance(zp, dict):
                parsed = parse_plan(zp)
                if parsed:
                    pid = parsed["plan_id"]
                    existing = next((p for p in plans if p["plan_id"] == pid), None)
                    if existing is not None:
                        existing.update(parsed)
                    else:
                        plans.append(parsed)
                        seen_ids.add(pid)
            # 2. 检查 claim_zcode_plan 按钮动作
            action = obj.get("action")
            if isinstance(action, dict) and action.get("type") == "claim_zcode_plan":
                args = action.get("args") or {}
                pid = str(args.get("plan_id") or args.get("planId") or "").strip()
                if pid and pid not in seen_ids:
                    plans.append({
                        "plan_id": pid,
                        "name": pid,
                        "description": "活动投放套餐",
                        "priority": 100,
                        "grants": [],
                    })
                    seen_ids.add(pid)
            for v in obj.values():
                _scan(v)
        elif isinstance(obj, list):
            for item in obj:
                _scan(item)

    for delivery in deliveries or []:
        _scan(delivery)

    return plans


async def fetch_marketing_touch_plans(account: Account) -> list[dict]:
    """拉取 marketing/touch 接口下发的活动套餐（2026-11 官方实证）。

    失败时仅记录并返回空列表，不阻断主流程。
    """
    from .quota import _auth_headers

    try:
        body = await _billing_request(
            account, "GET", settings.ZCODE_MARKETING_TOUCH_URL,
            headers=_auth_headers(account),
            params={"seq": "0"},
        )
    except Exception as err:
        logs.info("claim", f"账号 {account.name} marketing/touch 查询跳过（{err}）")
        return []

    code = _business_code(body)
    if code != 0:
        return []
    deliveries = (body.get("data") or {}).get("deliveries") or []
    return extract_plans_from_marketing_deliveries(deliveries)


async def _billing_request(account: Account, method: str, path: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers"))
    url = path if path.startswith("http") else f"{settings.ZCODE_BILLING_BASE}{path}"
    # 瞬态网络重试（2.7.68/2.7.71/2026-11-21 高可用迭代）：连接/读超时是出口
    # 链路瞬态（EasyTier 重启窗口、家宽线抖动，tebi 实锤多次），单发即败会让
    # 本轮整号报废（-1 非信誉型，wrapper 不重试）。GET/HEAD 幂等可安全重试；
    # POST claim 是写操作，超时后上游可能已入账，重发会撞 1003——保持单发。
    # 2.7.71 提到 3 次（2s/4s 阶梯）仍不够：08:10-08:13 出口抖动是**分钟级**
    # （49 条探针/连接失败实锤），6s 总窗的重试全落抖动窗内白烧，号报废要等
    # 下一窗口 +3600s（白天窗口 0,7,8 内甚至没有下一轮）。提到 5 次阶梯
    # 2/4/8/15s（总窗 ~29s + 请求本身）吃住分钟级抖动的尾部。
    attempts = 5 if method.upper() in ("GET", "HEAD") else 1
    try:
        # 出口由 settings.BILLING_EGRESS 决定（trust_env 按 direct/proxy 切）。
        # 2026-11-13 tebi 实证：GCP 大厂 ASN 直连可用（历史缺省），机房小 ASN
        # 直连 IP 会被 billing 风控拒（preview 空投放/429、验证码 3007）——
        # 走家宽代理 3/3 号立即可领。messages 端点与此独立（egress.py 判定）。
        # per-account 出口隔离（2.7.36）：账号绑定专属代理时优先——billing 与
        # messages 必须同 IP（token/风控同源）。
        account_proxy = egress.account_proxy(account)
        trust_env = settings.BILLING_EGRESS == egress.PROXY
        client_kw = ({"proxy": account_proxy, "trust_env": False} if account_proxy
                     else {"trust_env": trust_env})
        async with httpx.AsyncClient(timeout=25, **client_kw) as client:
            res: httpx.Response | None = None
            for attempt in range(1, attempts + 1):
                try:
                    res = await client.request(
                        method, url,
                        headers=headers, **kwargs,
                    )
                    break
                except httpx.TransportError as err:
                    if attempt >= attempts:
                        raise
                    wait = (2, 4, 8, 15)[attempt - 1]
                    logs.info("claim",
                              f"账号 {account.name} billing {method} 瞬态网络错误"
                              f"（{type(err).__name__}），{wait}s 后重试"
                              f"（{attempt}/{attempts - 1}）")
                    await asyncio.sleep(wait)
        assert res is not None  # 循环必经 break/raise
    except httpx.HTTPError as err:
        # 连接/超时等网络故障统一转业务错误：路由层只需面对 ClaimError 一种失败
        raise ClaimError(f"上游网络错误: {err}") from err
    if res.status_code in (401, 403):
        text = (res.text or "").lower()
        is_challenge = "captcha" in text or "verify" in text
        if not is_challenge:
            # 403 + 风控文案 ≠ 凭证失效：标 INVALID 会把健康号永久踢出领取链
            #（与 messages 链 3012 判定同一口径，2.7.33）。
            is_risk = res.status_code == 403 and any(
                m.lower() in text for m in constants.RISK_CONTROL_MARKERS
            )
            if is_risk:
                raise ClaimError(
                    f"上游风控拒绝 HTTP 403（非凭证失效）: {' '.join((res.text or '').split())[:160]}",
                    code=403,
                )
            _mark_auth_failure(account)
            raise ClaimError(AUTH_EXPIRED_MESSAGE)
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
    套餐投放资格信号）。事件体/端点/业务码判定收敛在 telemetry 单一事实源；
    请求无 Authorization（上游事件端点不校验）；任何失败仅返回文案，不阻断
    preview，首个失败即中止（日活键在上游按 device_mid+日期去重，重试无意义）。
    """
    from .fingerprint import profile_for
    from .telemetry import post_activation_event

    profile = profile_for(account)
    user_id = jwt_user_id(account)
    if not user_id:
        return "JWT 无 user_id，跳过激活上报"
    for element in constants.ACTIVATION_ELEMENTS:
        try:
            await post_activation_event(profile, user_id, element,
                                        proxy=egress.account_proxy(account))
        except (httpx.HTTPError, RuntimeError) as err:
            return _activation_err_text(element, err)
    return None


def _activation_err_text(element: str, err: Exception) -> str:
    """激活失败文案。httpx 的部分异常（ReadError 等）str 为空——空串时回退
    异常类名，否则日志只剩「上报失败: 」查无可查（2026-11-03 线上实锤）。"""
    detail = str(err).strip() or type(err).__name__
    return f"激活事件 {element} 上报失败: {detail}"


# 激活上报每日去重门：账号 id → 最近一次**成功**上报的本地日期。
# 自动路径（领取轮/入池）每号每天最多成功上报一次（官方客户端 app_launch
# 语义 = 每次启动一次；日活键上游本就按 device_mid+日期去重）。上报失败不
# 记账，下一轮自然重试（轮间隔 3600s，故障期上限 24 次/天，自愈）。手动
# preview 端点不走此门（用户触发 = zcode-switch claim_refresh 语义）。
_ACTIVATION_REPORTED: dict[str, str] = {}


def _today_key() -> str:
    """北京时区当日键（2.7.68 口径统一）：激活日门此前用宿主本地时区，与领取
    窗口的 gmtime+8 口径分裂——host 换 UTC 后日门翻转点从北京 0 点漂成 08:00，
    「每天 19:00 放量」的资格信号（app_daily_active）会在窗口中途才重置。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


# 同账号领取互斥（2.7.68）：手动端点 / 领取轮 / 入池任务三条链可对同一账号
# 并发跑 auto_claim_all_plans / claim——双链并发会 captcha 池争抢 + billing
# 双写重复打上游，且另一条链的 rebalance 插入「求解完成 → claim POST」窗口
# 时，token 的签发 IP 与请求 IP 漂移（上游判 unusual activity）。三条链的
# 账号段全部经此锁串行（锁只包账号段，跨账号天然并行）。
_ACCOUNT_CLAIM_LOCKS: dict[str, asyncio.Lock] = {}


def account_claim_lock(account_id: str) -> asyncio.Lock:
    return _ACCOUNT_CLAIM_LOCKS.setdefault(account_id, asyncio.Lock())


async def auto_claim_all_plans(account: Account, *, skip_plan_ids: set[str] | None = None) -> list[dict]:
    """新账号入池自动领取：每日一次激活上报（preview 之前）→ preview → 逐个领取。

    入池链路的 fire-and-forget 收尾：任何失败只记日志/返回 outcome，绝不抛出
    （入池流程不受影响）。重复执行安全（上游 1003 已领取过幂等）。
    skip_plan_ids：本轮跳过领取的套餐 id（1005 名额等待期由 ClaimRoundManager
    记忆传入）；preview 仍照常执行，新套餐发现不受影响。

    激活上报时序（2.6.6 根因修复）：上报必须先于 preview——活动投放疑似以
    当日激活信号为资格（constants.ACTIVATION_ELEMENTS 注释、09-06 实证），
    「先 preview 后补上报」会在新的一天形成 preview 恒空 → 永不激活的死锁。
    频率由每日去重门控制（每号每天 1 次，官方节奏），不再每轮空发。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        return []
    # 同账号三条领取链互斥（2.7.68）：锁只包账号段，跨账号天然并行
    async with account_claim_lock(account.id):
        return await _auto_claim_all_plans_locked(account, skip_plan_ids=skip_plan_ids)


async def _auto_claim_all_plans_locked(account: Account, *,
                                       skip_plan_ids: set[str] | None = None) -> list[dict]:
    """auto_claim_all_plans 的锁内实现（同号互斥语义见外层函数）。"""
    outcomes: list[dict] = []

    if _ACTIVATION_REPORTED.get(account.id) != _today_key():
        try:
            err = await report_activation_events(account)
            if err:
                logs.warn("claim", f"账号 {account.name} 激活上报失败: {err}")
            else:
                _ACTIVATION_REPORTED[account.id] = _today_key()
        except Exception as err:  # noqa: BLE001 - 激活失败不阻断领取
            logs.warn("claim", f"账号 {account.name} 激活上报异常: {err}")

    try:
        plans = await preview_plans(account)
    except ClaimError as err:
        logs.info("claim", f"账号 {account.name} 无可领套餐（{err}）")
        return outcomes
    except Exception as err:  # noqa: BLE001
        logs.warn("claim", f"账号 {account.name} preview 异常: {err}")
        return outcomes

    if not plans:
        logs.info("claim", f"账号 {account.name} 上游无投放套餐，跳过领取")
        return outcomes

    skip = skip_plan_ids or set()
    skipped = 0
    for plan in plans:
        if plan["plan_id"] in skip:
            skipped += 1
            continue
        try:
            result = await claim_with_egress_retry(account, plan["plan_id"])
            outcomes.append({"account_id": account.id, "account_name": account.name,
                             "ok": True, **result})
            logs.ok("claim", f"账号 {account.name} 自动领取成功: "
                             f"{result.get('plan_name') or plan['plan_id']}")
        except ClaimError as err:
            if err.code == 1003:
                # 「已经领取过」= 幂等成功：上游投放会重复列出同一 plan，
                # 把 1003 记成失败会让每轮都报错（2.7.33）
                outcomes.append({"account_id": account.id, "account_name": account.name,
                                 "ok": True, "already_claimed": True,
                                 "plan_id": plan["plan_id"], "message": str(err)})
                logs.info("claim", f"账号 {account.name} 套餐 {plan['plan_id']} 已领取过，跳过")
                continue
            outcome = {"account_id": account.id, "account_name": account.name,
                       "ok": False, "plan_id": plan["plan_id"], "message": str(err)}
            if err.code != -1:
                outcome["code"] = err.code
            if err.next_at:
                outcome["next_at"] = err.next_at
            outcomes.append(outcome)
            logs.warn("claim", f"账号 {account.name} 自动领取 {plan['plan_id']} 失败: {err}")
            if err.code in (3007, 403):
                # 验证码被拒（3007）/ 上游风控 403（unusual activity 文案实锤，
                # _billing_request 只在命中 RISK_CONTROL_MARKERS 时才发 code=403）
                # 都是出口 IP 风险信号，403 的证据强度不亚于 3007——本账号本轮
                # 不再打第二个套餐（2.7.68 把 403 补进止损集：此前多套餐投放期
                # 403 后循环继续，同坏出口每个套餐都白烧一枚码）。
                break
        except Exception as err:  # noqa: BLE001
            logs.warn("claim", f"账号 {account.name} 自动领取异常: {err}")
            outcomes.append({"account_id": account.id, "account_name": account.name,
                             "ok": False, "plan_id": plan["plan_id"], "message": str(err)})
            # 求解失败/换绑后仍信誉不过（含 wrapper 换绑止损路径）：本账号本轮
            # 不再打第二个套餐——同一坏出口上每个套餐都要再烧一枚码（2.7.50
            # review：与 3007 break 同口径）
            if isinstance(err, CaptchaSolveError):
                break
    if skipped:
        logs.info("claim", f"账号 {account.name} {skipped} 个套餐名额等待期，本轮跳过领取")
    return outcomes


async def preview_plans(account: Account) -> list[dict]:
    """拉取账号当前可领取套餐，按优先级降序。

    双通道发现：
    1. /billing/preview（常规预览端点）
    2. /api/v1/marketing/touch（活动投放端点，2026-11 官方实证）
    两者套餐按 plan_id 去重合并并按优先级降序排列。
    """
    blocked = billing_block_reason(account, action="上游查询")
    if blocked:
        raise ClaimError(blocked)
    from .fingerprint import profile_for
    from .quota import _auth_headers

    body = await _billing_request(
        account, "GET", "/billing/preview",
        headers=_auth_headers(account),
        # platform 跟账号档案走（官方 TH() = process.platform-arch）；
        # 实测 client/configs 才拒 platform 参数，preview 宽容。
        params={"app_version": constants.BILLING_APP_VERSION,
                "platform": profile_for(account).platform_full},
    )
    code = _business_code(body)
    if code != 0:
        raise _fail_error(code, body)
    raw_plans = (body.get("data") or {}).get("plans") or []
    plans = [parsed for parsed in (parse_plan(p) for p in raw_plans) if parsed]

    # 合并 marketing/touch 活动投放通道（官方客户端实证：活动套餐由此下发）
    try:
        marketing_plans = await fetch_marketing_touch_plans(account)
        seen_ids = {p["plan_id"] for p in plans}
        for mp in marketing_plans:
            if mp["plan_id"] not in seen_ids:
                plans.append(mp)
                seen_ids.add(mp["plan_id"])
    except Exception as err:
        logs.warn("claim", f"账号 {account.name} marketing/touch 套餐合并异常: {err}")

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
    # 兜底防止基座头漂移）。平台必须跟账号档案走，禁止再盖成全局 darwin-arm64。
    headers["X-ZCode-App-Version"] = constants.BILLING_APP_VERSION
    return headers


async def _post_claim(account: Account, headers: dict, plan_id: str) -> dict:
    """提交 billing/claim 并翻译业务码。"""
    body = await _billing_request(
        account, "POST", "/billing/claim",
        headers=headers, json={"plan_id": plan_id},
    )
    code = _business_code(body)
    if code != 0:
        raise _fail_error(code, body)
    return body


def _claim_outcome(body: dict, plan_id: str, plan_name: str, grants: list) -> dict:
    """领取成功返回集；server_time/starts_at/ends_at 为上游秒值 → 毫秒
    （zcode-switch 3.11.2 领取语义：服务端时钟随成功载荷下发，供前端
    区分本机时钟漂移）。缺失字段保持 None，不造数。"""
    data = body.get("data") or {}
    plan = data.get("plan") or {}

    def _ms(key: str) -> int | None:
        val = plan.get(key)
        return int(val * 1000) if isinstance(val, (int, float)) and val > 0 else None

    server_time = data.get("server_time")
    resp_name = str(plan.get("name") or "").strip()
    parsed_grants = grants or (parse_plan(plan) or {}).get("grants", [])
    return {
        "plan_id": plan_id,
        "plan_name": resp_name or plan_name or plan_id,
        "grants": parsed_grants,
        "starts_at": _ms("starts_at"),
        "ends_at": _ms("ends_at"),
        "server_time": int(server_time * 1000) if isinstance(server_time, (int, float)) and server_time > 0 else None,
    }


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
    blocked = billing_block_reason(account)
    if blocked:
        raise ClaimError(blocked)
    if not (verify_param or "").strip():
        raise ClaimError("缺少验证码参数，请先完成人机验证")

    plan_id, plan_name, grants = await _auto_pick_plan(account, plan_id or None)
    headers = _claim_headers(account, verify_param.strip(), region)
    body = await _post_claim(account, headers, plan_id)
    return _claim_outcome(body, plan_id, plan_name, grants)


async def claim(account: Account, plan_id: str | None = None) -> dict:
    """领取套餐。plan_id 缺省时自动选优先级最高的可领套餐。

    返回 _claim_outcome 形态（含 server_time/starts_at/ends_at，可能为 None）；
    1005 携带 next_at（名额恢复时间）。验证码被拒（3007）单次即止（2.6.5 整改）：
    拒码本身就是上游风险信号，换码连打只会加剧——拒码出池，下一轮自然重试。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        raise ClaimError("仅 Coding Plan (JWT) 账号支持领取")
    blocked = billing_block_reason(account)
    if blocked:
        raise ClaimError(blocked)

    plan_id, plan_name, grants = await _auto_pick_plan(account, plan_id)
    # claim 的 billing 请求出口见 _billing_request（BILLING_EGRESS / 账号绑定代理）；
    # 验证码必须同出口解，否则 token 的出口与请求出口不一致，上游判 unusual activity。
    # per-account 模式（2.7.36）：tag/proxy 都是账号绑定代理 URL。
    claim_egress = egress.egress_tag_for(account, settings.BILLING_EGRESS)
    claim_proxy = egress.account_proxy(account)
    verify_param, verify_region = await captcha_manager.get_verify_param(
        egress_tag=claim_egress, proxy=claim_proxy,
        # 领取链求解预算解耦（2.7.68）：单号最坏 4×240s 会把整晚窗口吃穿，
        # claim 路径缺省 2 次起（settings.CAPTCHA_CLAIM_RETRIES 可调）
        retries=settings.CAPTCHA_CLAIM_SOLVE_RETRIES,
    )
    config = await captcha_manager.fetch_config(claim_egress, claim_proxy)
    headers = _claim_headers(account, verify_param, verify_region or config.get("region"))

    body = await _billing_request(
        account, "POST", "/billing/claim",
        headers=headers, json={"plan_id": plan_id},
    )
    code = _business_code(body)
    if code == 0:
        return _claim_outcome(body, plan_id, plan_name, grants)
    if code == 3007:
        captcha_manager.invalidate(claim_egress)  # 被拒的码出池，避免下轮复用
    raise _fail_error(code, body)


# IP 信誉驱动重试（2026-11-16 活动套餐实弹定案）：claim 成败强跟随出口节点——
# 同一坏节点上重试 4 次验证码也是同一个 IP，白烧求解与 claim 写流量。三类
# 失败归因 IP 信誉：①HTTP 403 风控（unusual activity 文案）；②验证码求解
# 失败（solver 是最灵敏的 IP 探针——F001 全拒=节点在阿里云侧已烂）；③3007
# 换码仍拒。命中即 rebalance 换绑（ban 旧节点带 billing 证据），换绑后用
# 「验证码现解成功」作真实验收（rebalance 自带的 Plan 探针只验 messages 通
# 道，不验 billing/阿里云维度），验收过才重打 claim。
# env 解析收编 settings（2026-11-17 深度迭代）：import 崩溃风险与全仓口径统一。
CLAIM_EGRESS_RETRIES = settings.CLAIM_EGRESS_RETRIES
_EGRESS_RETRY_DELAY_S = settings.CLAIM_EGRESS_RETRY_DELAY


def _is_egress_reputation_failure(err: Exception) -> bool:
    """判定是否 IP 信誉型失败（值得换绑重试，而非套餐/参数/凭证问题）。"""
    if isinstance(err, ClaimError):
        if err.code == 3007:
            return True
        # 403 必须「风控文案实锤」才换绑（2.7.50 review P1）：_billing_request
        # 只在命中 RISK_CONTROL_MARKERS 时才发 code=403，但防御纵深不可省——
        # 若上游文案漂移/新增 403 语义混进来，盲目换绑会白烧 2 轮 rebalance +
        # 4 次浏览器求解（每号每轮 10+ 分钟）而换 IP 根本无解。3001/3002 是
        # 频率/参数维度（3002 频率风控也非 IP 信誉，换 IP 无用）。
        if err.code == 403:
            text = str(err).lower()
            return any(m.lower() in text for m in constants.RISK_CONTROL_MARKERS)
        return False
    # captcha_manager.get_verify_param 抛的求解失败：出口不通/节点被阿里云拒
    return isinstance(err, CaptchaSolveError)


async def _rebalance_for_claim(account: Account, reason: str) -> bool:
    """为 claim 换绑出口节点；换绑后用验证码现解做真实验收。

    Returns True = 已换到新节点且验证码求解通过（IP 信誉已恢复）；
    False = 无 per-account 出口可换 / rebalance 失败 / 验收仍不过 /
    换绑环节自身异常（吞掉并记日志——不掩盖原始 claim 错误上抛，2.7.50
    review P2：本函数在 wrapper 的 except 块内被 await，穿出的异常会替换
    原 ClaimError，归因链断裂）。"""
    from .egress_pool import egress_pool

    claim_proxy = egress.account_proxy(account)
    if not claim_proxy:
        return False  # 直连/共享出口：无节点可换
    old_tag = egress.egress_tag_for(account, settings.BILLING_EGRESS)
    old_node = account.egress_node
    captcha_manager.invalidate(old_tag)  # 旧 IP 池内的码全部作废（换绑失败时
    #  池已污染；换绑成功后 egress_pool 还会对同一 tag 再 invalidate 一次，
    #  双清无害——两处清的都是「旧底层 IP 签发的码」）

    try:
        # TP ban reason 是闭合枚举（edge_405_html | unusual_activity_3012 |
        # rate_limited_429_burst，见 TetherPool API 需求 §ban），自由文本 422。
        # ⚠️ unusual_activity_3012 被 TP 服务端强制 quarantine_hours=0（只记录
        # 不隔离）——2026-11-17 实弹实证：当晚 8 条「ban 已隔离」日志实际全部
        # 未隔离，烂节点持续被 force_rebalance 分回，claim 换绑死循环的帮凶。
        # captcha 信誉失败（403 unusual activity / 求解 F001 / 3007）是 IP 级
        # 封禁信号，必须传唯一真隔离的 edge_405_html；细粒度归因放 evidence
        # （rebalance 自带）。
        ok = await egress_pool.rebalance(
            account, "edge_405_html", ban=True
        )
        if not ok or account.egress_node == old_node:
            logs.warn("claim", f"账号 {account.name} 换绑未生效（{old_node or '?'}），放弃本轮重试")
            return False

        # 验收：新节点上现解一枚验证码。这一步同时完成①IP 连通性②阿里云对该
        # 新 IP 的信誉评分。失败 = 新节点同样烂，不重打。预算走领取链口径
        # （2.7.68）：验收是决策探针不是可用性兜底，跑满 4 次只会拖长换绑链。
        new_tag = egress.egress_tag_for(account, settings.BILLING_EGRESS)
        await captcha_manager.get_verify_param(egress_tag=new_tag,
                                               proxy=egress.account_proxy(account),
                                               retries=settings.CAPTCHA_CLAIM_SOLVE_RETRIES)
        logs.ok("claim", f"账号 {account.name} 新出口 {account.egress_node} 验证码验收通过")
        return True
    except CaptchaSolveError as err:
        logs.warn("claim", f"账号 {account.name} 新出口 {account.egress_node} "
                           f"验证码验收失败: {err}——节点信誉不足，不再重打")
        return False
    except Exception as err:  # noqa: BLE001 - 换绑环节异常不掩盖原始 claim 失败
        logs.warn("claim", f"账号 {account.name} 换绑环节异常: "
                           f"{type(err).__name__}: {str(err)[:120]}——放弃本轮重试")
        return False


async def claim_with_egress_retry(account: Account, plan_id: str | None = None) -> dict:
    """claim 的 IP 信誉驱动包装：信誉型失败 → 换绑 → 验收 → 重打。

    非 IP 信誉型失败（1001-1005/3001/3002/凭证失效）原样上抛，行为与裸
    claim() 完全一致；换绑重试上限 ZCODE_CLAIM_EGRESS_RETRIES（缺省 2），
    每次重试间隔 ZCODE_CLAIM_EGRESS_RETRY_DELAY（缺省 20s，给 TP ban/新
    allocation 生效留缓冲）。"""
    attempts = CLAIM_EGRESS_RETRIES + 1
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await claim(account, plan_id)
        except Exception as err:  # noqa: BLE001 - 统一归因判定后分类处理
            last_err = err
            if attempt >= attempts or not _is_egress_reputation_failure(err):
                raise
            logs.warn("claim",
                      f"账号 {account.name} 第 {attempt}/{attempts} 次 claim 遇 IP 信誉型失败"
                      f"（{type(err).__name__}: {str(err)[:120]}）→ 换绑出口重试")
            try:
                rebalanced = await _rebalance_for_claim(account, str(err)[:80])
            except Exception as rb_err:  # noqa: BLE001 - 换绑异常不掩盖原始失败
                logs.warn("claim", f"账号 {account.name} 换绑调度异常: "
                                   f"{type(rb_err).__name__}: {str(rb_err)[:120]}"
                                   f"——上抛原始 claim 错误")
                raise err from err  # 显式上抛原始 ClaimError/CaptchaSolveError，非换绑异常
            if not rebalanced:
                raise
            await asyncio.sleep(_EGRESS_RETRY_DELAY_S)
    raise last_err  # pragma: no cover - 循环必经 return/raise


class ClaimRoundManager:
    """后台周期自动领取轮（默认 1 小时，2.6.5 整改；对齐 zcode-switch 用户触发语义）。

    每轮对全部可打 billing 的 JWT 账号执行 auto_claim_all_plans（每日一次激活
    上报先于 preview + 逐个领取可领套餐）；只有「无 JWT / JWT 失效（INVALID）」
    会被 allows_billing() 先行过滤——**停用、风控禁用、冷却中的号照常参与领取**
    （领取与使用两条链，2026-11-04 用户口径；冷却期会因此产生 billing 流量，
    但绝不产生 messages 流量）。
    1005（今日名额用完）按服务端 next_at 退避：等待期该套餐不打 claim
    （省 captcha 求解与上游写流量），preview 照常保留新套餐发现
    （docs/development/05「按服务端 next window 退避」语义落地）。
    轮间隔与**领取窗口**（claim_round_hours，北京时间小时）运行期读 meta 设置
    （interval 0 = 关闭；窗口外只睡不跑、到点准时醒来；空窗口 = 全天）；
    单账号异常不中断整轮，后台任务异常全部自兜。
    """

    # 停服截止：轮内长尾（验证码求解重试可达分钟级）超时即 cancel，
    # uvicorn lifespan 关闭保持有界，不被半途的求解卡成 SIGKILL 脏停机
    STOP_GRACE_SECONDS = 10

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 账号 → {plan_id: 名额恢复时间(ms)}：1005 退避记忆（仅轮次路径使用）。
        # 落库持久化（2026-11-22 review P2 根修）：纯内存态重启即失忆——按
        # plan_guard 2.7.55 对「重启失忆」的定性与一晚 9 次 SIGKILL 重启史，
        # 每次重启都会对全部 1005 等待期套餐重发 claim（烧一枚验证码求解 +
        # 一次 billing/claim POST 后重收 1005）。落 store meta，启动回读。
        self._claim_holds: dict[str, dict[str, int]] = self._load_holds()
        self._holds_persisted: dict[str, dict[str, int]] = {}  # 已落库影子（增量写盘）
        # 账号 → 上次被预算跳过的 monotonic 时刻（轮转公平排序键，2026-11-22）
        self._starved_at: dict[str, float] = {}
        # 无可服务账号的告警只在状态变化时发（持续故障期不逐轮刷屏）
        self._empty_warned = False

    # ── 1005 退避表持久化（2026-11-22 review P2）───────────────────────────
    _HOLDS_META_KEY = "claim_holds_1005"

    def _load_holds(self) -> dict[str, dict[str, int]]:
        """启动回读 1005 退避表（per-account meta 键）；损坏/缺失按空表处理
        （幂等重探一次可接受）。"""
        try:
            from .store import store
            out: dict[str, dict[str, int]] = {}
            for key, val in store.all_settings().items():
                if not key.startswith(f"{self._HOLDS_META_KEY}:"):
                    continue
                aid = key.split(":", 1)[1]
                if not val:
                    continue
                try:
                    pl = json.loads(str(val))
                except ValueError:
                    continue
                if isinstance(pl, dict):
                    out[aid] = {str(k): int(v) for k, v in pl.items()
                                if isinstance(v, (int, float))}
            return out
        except Exception:  # noqa: BLE001 - 持久层故障不阻断启动
            return {}

    def _persist_holds(self, holds: dict[str, dict[str, int]]) -> None:
        """增量落库：仅写与影子不同的账号条目（每轮通常 ≤N 条小 JSON）。"""
        try:
            from .store import store
            for aid, pl in holds.items():
                if self._holds_persisted.get(aid) != pl:
                    store.set_setting(f"{self._HOLDS_META_KEY}:{aid}",
                                      json.dumps(pl, separators=(",", ":")) if pl else "")
                    self._holds_persisted[aid] = dict(pl)
        except Exception as err:  # noqa: BLE001 - 落库失败退化为旧行为（内存态）
            logs.warn("claim", f"1005 退避表落库失败（本轮内存态继续）: {err}")

    async def _round(self) -> None:
        from .quota import refresh_accounts
        from .store import store

        accounts = [
            a for a in store.list_accounts("zai")
            if a.mode == "jwt" and a.allows_billing()
        ]
        if not accounts:
            if not self._empty_warned:
                logs.warn("claim", "无可领取账号（无 JWT 或 JWT 已失效），本轮跳过")
                self._empty_warned = True
            return
        self._empty_warned = False
        # 轮次执行前先轻量核验官方最新版本（若有新版本且开启 auto_sync，自动对齐指纹）
        try:
            from .version_tracker import version_tracker

            await version_tracker.check_and_sync()
        except Exception as err:  # noqa: BLE001
            logs.info("claim", f"轮次前官方版本核验跳过（{err}）")

        now_ms = time.time() * 1000
        # 退避表先清过期项（next_at 已过的恢复本轮重试）
        holds = {
            aid: {pid: at for pid, at in pl.items() if at > now_ms}
            for aid, pl in self._claim_holds.items()
        }
        self._claim_holds = holds
        # 轮级 wall-clock 预算（2.7.68）：单号最坏 ≈90 分钟（求解长尾 × 换绑链），
        # 11 号串行会把 19-23 点窗口吃穿（三晚 0 成功的结构性放大器）。预算到点
        # 后剩余账号本轮跳过（下拍续领），保证轮在窗口内收尾。
        budget = float(settings.CLAIM_ROUND_BUDGET or 0)
        deadline = time.monotonic() + budget if budget > 0 else None
        concurrency = max(1, int(settings.CLAIM_ROUND_CONCURRENCY))
        skipped_by_budget = 0
        # 轮转公平起点（2026-11-22 review P2 根修）：并发槽 Semaphore(concurrency)
        # FIFO 下排前的账号先拿槽，预算耗尽时同一批尾部账号每轮都被跳过——
        # 缺省 19 点单小时窗口 + interval 3600s 下窗口内恰好只跑一轮 = 尾部号
        # 结构性饥饿 23h。现把上轮预算跳过的账号排到队首（_starved_at 记轮次），
        # 饥饿不跨轮累积。
        accounts.sort(key=lambda a: self._starved_at.get(a.id, 0.0))

        async def _claim_one(acc: Account, acc_holds: dict[str, int]) -> bool:
            """单账号领取段：预算内执行 + 退避表更新 + 入账刷新。返回是否入账。"""
            nonlocal skipped_by_budget
            if deadline is not None and time.monotonic() >= deadline:
                skipped_by_budget += 1
                self._starved_at[acc.id] = time.monotonic()
                logs.info("claim",
                          f"账号 {acc.name} 本轮预算耗尽跳过（轮级预算 "
                          f"{int(budget)}s 已用完，下拍续领·下轮优先）")
                return False
            if acc_holds:
                soon = max(0, (min(acc_holds.values()) - now_ms) / 1000)
                logs.info("claim", f"账号 {acc.name} {len(acc_holds)} 个套餐名额等待中"
                                   f"（约 {int(soon)}s 后恢复重试）")
            # 单号硬截止 = 轮级剩余预算（求解长尾被 wait_for 兜住，不再拖垮整轮）
            left = (deadline - time.monotonic()) if deadline is not None else None
            try:
                outcomes = await asyncio.wait_for(
                    auto_claim_all_plans(acc, skip_plan_ids=set(acc_holds)),
                    timeout=left,
                )
            except TimeoutError:
                logs.warn("claim",
                          f"账号 {acc.name} 本轮领取超轮级预算被截断（下拍续领）")
                return False
            except Exception as err:  # noqa: BLE001 - 单账号异常不中断整轮
                logs.warn("claim", f"轮次领取 账号 {acc.name} 异常: {err}")
                return False
            # 按本轮实绩更新退避表：1005+next_at 记忆；成功/其余失败清除
            # （被跳过的套餐无 outcome，等待自然延续到 next_at）
            updated = dict(acc_holds)
            for o in outcomes:
                pid = o.get("plan_id")
                if not pid:
                    continue
                next_at = o.get("next_at") if o.get("code") == 1005 else None
                if next_at:
                    updated[pid] = int(next_at)
                else:
                    updated.pop(pid, None)
            if updated:
                holds[acc.id] = updated
            else:
                holds.pop(acc.id, None)
            if not any(o.get("ok") for o in outcomes):
                return False
            live = store.find("zai", acc.id)
            if live is not None:
                try:
                    await refresh_accounts([live])  # 领到额度立即反映到 UI
                except Exception as err:  # noqa: BLE001
                    logs.warn("claim", f"轮次领取 账号 {acc.name} 额度刷新失败: {err}")
            return True

        # 并发调度（2.7.68）：求解进程级上限与 TP rebalance 互斥都有自己的全局
        # 闸，这里的并发只是调度并行度，不放大浏览器/上游压力。
        claimed = 0
        sem = asyncio.Semaphore(concurrency)

        async def _worker(acc: Account) -> None:
            nonlocal claimed
            async with sem:
                if await _claim_one(acc, holds.get(acc.id, {})):
                    claimed += 1

        await asyncio.gather(*[_worker(acc) for acc in accounts], return_exceptions=True)
        if skipped_by_budget:
            logs.info("claim",
                      f"本轮 {skipped_by_budget} 个账号因轮级预算耗尽未执行"
                      f"（下拍续领·下轮队首优先）")
        if claimed:
            logs.ok("claim", f"自动领取轮完成: {claimed} 个账号有新套餐入账")
        # 退避表增量落库（2026-11-22 review P2）：重启不再失忆，等待期套餐
        # 不因发版/SIGKILL 重启白烧求解+claim 流量
        self._persist_holds(holds)

    async def _loop(self) -> None:
        from .store import store

        # 启动先等一段（避开启动安装序 / 入池自动领取的首次流量高峰）
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.claim_round_interval()  # 实时读取设置，改后即生效
            hours = store.claim_round_hours()
            if self._should_run(interval, hours):
                try:
                    await self._round()
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("claim", f"自动领取轮出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._next_wait(interval, hours))
                return
            except TimeoutError:
                continue

    @classmethod
    def _should_run(cls, interval: int, hours: list[int], now: float | None = None) -> bool:
        """本轮是否执行：轮次启用（interval>0）且（无窗口限制 或 当前在窗口内）。"""
        return interval > 0 and cls._in_window(hours, now)

    # ── 领取窗口调度（北京时间）─────────────────────────────────────────────
    # 上游套餐按天投放，用户侧实证每天 19:00 放量。窗口锚定北京时钟而非 host
    # 时区（host 换成 UTC 也不漂）；中国无夏令时，固定 +8 即精确，无 tzdata 依赖。

    @staticmethod
    def _beijing_hour_frac(now: float | None = None) -> float:
        """当前北京时间的小时（含小数，19.5 = 19:30）。"""
        lt = time.gmtime((now if now is not None else time.time()) + 8 * 3600)
        return lt.tm_hour + lt.tm_min / 60 + lt.tm_sec / 3600

    @classmethod
    def _in_window(cls, hours: list[int], now: float | None = None) -> bool:
        """当前北京时间小时是否在领取窗口内（空窗口 = 全天不限时）。"""
        if not hours:
            return True
        return int(cls._beijing_hour_frac(now)) in hours

    @classmethod
    def _seconds_until_window(cls, hours: list[int], now: float | None = None) -> float:
        """距下一个窗口起点（北京时间整点）的秒数；已在窗口内返回 0。"""
        frac = cls._beijing_hour_frac(now)
        if int(frac) in hours:
            return 0.0  # 恰逢整点（frac == hour）也属于窗口内，不能落进「今天已过」
        for hour in sorted(hours):
            if frac < hour:
                return (hour - frac) * 3600
        return (24 - frac + min(hours)) * 3600  # 今天窗口已过 → 明天第一个窗口

    @classmethod
    def _next_wait(cls, interval: int, hours: list[int], now: float | None = None) -> float:
        """下一次唤醒秒数。

        interval<=0：关闭态 30s 回看设置（随时可启用）；
        无窗口：interval 节拍；有窗口：窗口内 interval 节拍，窗口外最迟 30min
        回看一次设置、并在窗口开始时准时醒来（19:00 放量即领，不空转一天）。
        """
        if interval <= 0:
            return 30.0
        if not hours:
            return float(interval)
        now = now if now is not None else time.time()
        if cls._in_window(hours, now):
            return float(interval)
        return max(30.0, min(1800.0, cls._seconds_until_window(hours, now)))

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止后台轮：事件置位后有截止地等待；超时即 cancel 轮任务。

        轮内正在跑的验证码求解/上游请求长尾（求解重试可达分钟级）不允许
        卡住 lifespan 关闭——到期 cancel，收尾即返回。
        """
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=self.STOP_GRACE_SECONDS)
        except TimeoutError:
            pass  # wait_for 已在截止时 cancel 并等待收尾
        except Exception as err:  # noqa: BLE001 - _loop 自兜；此处留痕防停服冒泡
            logs.err("claim", f"领取轮任务异常退出: {err}")
        self._task = None


claim_round = ClaimRoundManager()
