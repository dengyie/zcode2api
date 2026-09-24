"""后台管理 API：/admin/api/*（账号池、设置、用量监控）。"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from .. import logs, reqlog
from ..auth_admin import verify_admin_key
from ..captcha import CaptchaSolveError
from ..claim import (
    AUTH_EXPIRED_MESSAGE,
    ClaimError,
    auto_claim_all_plans,
    billing_block_reason,
    claim_with_captcha,
    preview_plans,
    report_activation_events,
)
from ..claim import claim as do_claim
from ..install import run_install_sequence_for_account
from ..models import PROVIDERS, Status
from ..oauth import ZaiAuthFlow
from ..quota import fetch_quota, refresh_accounts
from ..store import store

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])


# ── 鉴权探针 ─────────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify():
    return {"status": "ok"}


# ── 账号列表 + 概览统计 ──────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts():
    now = time.time()
    accounts = [a.public_view() for a in store.list_accounts()]
    stats = {"total": len(accounts), "active": 0, "exhausted": 0,
             "cooling": 0, "invalid": 0, "disabled": 0,
             "calls": 0, "fail": 0}
    for a in accounts:
        st = a["status"]
        if st in stats:
            stats[st] += 1
        stats["calls"] += a["use_count"]
        stats["fail"] += a["fail_count"]
    return {"accounts": accounts, "stats": stats, "providers": list(PROVIDERS), "ts": now}


@router.get("/status")
async def status_info():
    return {
        "providers": list(PROVIDERS),
        "gateway_key_set": bool(store.gateway_key()),
        "quota_pool": {
            p: sum(1 for a in store.list_accounts(p) if a.is_selectable())
            for p in PROVIDERS
        },
    }


# ── 新增账号 ─────────────────────────────────────────────────────────────────
@router.post("/accounts")
async def add_accounts(payload: dict = Body(...)):
    provider = payload.get("provider", "zai")
    if provider not in PROVIDERS:
        raise HTTPException(400, "不支持的 provider")
    tokens = payload.get("tokens") or []
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.splitlines() if t.strip()]
    tokens = [t.strip() for t in tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "请输入至少一个 Token / API Key")

    added = []
    existing = {a.id for a in store.list_accounts(provider)}  # 识别真新增（重复 token 跳过）
    for tok in dict.fromkeys(tokens):  # 去重保序
        name = payload.get("name") or f"{provider}-{len(store.list_accounts(provider)) + 1}"
        acc = store.add_account(provider, name, tok)
        added.append(acc.id)
    # 立即刷新一次额度（仅 zai jwt）
    fresh = [a for a in store.list_accounts(provider) if a.id in added and a.mode == "jwt"]
    if fresh:
        await refresh_accounts(fresh)
    # 真新增（id 不在加号前的集合里；重复 token 返回旧账号自动跳过）
    new_accounts = [a for a in store.list_accounts(provider)
                    if a.id in added and a.id not in existing]
    for acc in new_accounts:
        _schedule_install(acc)  # 按账号安装序（含 apiKey 账号，幂等）
        if acc.mode == "jwt":
            _schedule_auto_claim(acc)  # 授权完成即激活+自动领取，入池即吃满活动
    return {"count": len(added), "ids": added}


# ── 删除账号 ─────────────────────────────────────────────────────────────────
@router.delete("/accounts")
async def delete_accounts(ids: list[str] = Body(...)):
    deleted = 0
    for aid in ids:
        acc = store.find_any(aid)
        if acc and store.remove_account(acc.provider, aid):
            deleted += 1
    return {"deleted": deleted}


# ── 编辑账号 ─────────────────────────────────────────────────────────────────
@router.put("/accounts/{account_id}")
async def edit_account(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if "name" in payload and payload["name"]:
        acc.name = payload["name"].strip()
    secret = payload.get("token") or payload.get("secret")
    if secret:
        secret = secret.strip()
        is_jwt = secret.count(".") == 2 and acc.provider == "zai"
        if is_jwt:
            acc.mode = "jwt"
            acc.jwt_token = secret
            # 换 JWT 不清已有 API Key：OAuth 兑换出的回退通道要保住
        else:
            acc.mode = "apiKey"
            acc.api_key = secret
            acc.jwt_token = None
        acc.status = Status.ACTIVE
        acc.last_error = None
    store.update_account(acc)
    return {"ok": True}


# ── 启用 / 禁用 ──────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/enabled")
async def set_enabled(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    enabled = bool(payload.get("enabled", True))
    store.set_enabled(acc.provider, account_id, enabled)
    return {"ok": True}


# ── 客户端指纹（每账号独立设备档案）──────────────────────────────────────────
@router.post("/accounts/{account_id}/fingerprint/rotate")
async def rotate_fingerprint(account_id: str):
    """换发账号客户端指纹（下一套设备模板 + 全新 device_mid）。

    场景：账号被风控后换设备重生；或怀疑指纹污染时手动更换。
    """
    from ..fingerprint import profile_for, rotate

    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    old = profile_for(acc)
    profile = rotate(acc)
    acc.installed_at = None  # 换机后必须重跑安装序，旧 MID 的日活不能顶新设备
    store.update_account(acc)
    _schedule_install(acc)
    logs.info("fingerprint", f"账号 {acc.name} 指纹换发: "
                             f"{old.platform_full}/{old.device_mid[:8]} → "
                             f"{profile.platform_full}/{profile.device_mid[:8]}")
    return {"ok": True, "fingerprint": acc.fingerprint}


# ── 刷新额度（实时用量监控）─────────────────────────────────────────────────
@router.post("/accounts/refresh")
async def refresh(payload: dict = Body(default=None)):
    payload = payload or {}
    if payload.get("all"):
        pool = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    else:
        ids = set(payload.get("ids") or [])
        pool = [a for a in store.list_accounts() if a.id in ids and a.mode == "jwt"]
    # 失效 / 风控禁用 / 冷却 一律不打 billing（冷却期零上游流量 + 废 JWT 不再 401）
    targets = [a for a in pool if a.allows_billing()]
    skipped_cooling = sum(1 for a in pool if a.is_cooling())
    skipped_invalid = len(pool) - len(targets) - skipped_cooling
    summary = await refresh_accounts(targets)
    return {
        "summary": summary,
        "count": len(targets),
        "skipped_cooling": skipped_cooling,
        "skipped_invalid": skipped_invalid,
    }


@router.post("/accounts/{account_id}/refresh")
async def refresh_one(account_id: str):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.mode != "jwt":
        return {"ok": False, "message": "仅 Coding Plan (JWT) 账号支持额度查询"}
    blocked = billing_block_reason(acc, action="上游刷新")
    if blocked:
        return {"ok": False, "message": blocked, "account": acc.public_view()}
    res = await fetch_quota(acc)
    return {"ok": "error" not in res, "result": res, "account": acc.public_view()}


# ── OAuth 登录（Z.AI）────────────────────────────────────────────────────────
# 授权链接有效期：上游 cli/init 发起的流程约 5 分钟过期（zcode.z.ai 行为）。
# 会话过期后 poll 返回 {"status": "expired"}（幂等，可安全重试）。
LOGIN_FLOW_TTL = 300.0
# 兑换 API Key（getCustomerInfo → api_keys → copy）总时长上限
LOGIN_EXCHANGE_TIMEOUT = 60.0

# flow_id -> {"flow": ZaiAuthFlow, "created": float, "label": str}
# 单进程内存态即可：登录会话不该跨进程存活，重启后用户重新生成链接。
_login_flows: dict[str, dict] = {}


def _login_gc() -> None:
    now = time.time()
    expired = [fid for fid, entry in _login_flows.items() if now - entry["created"] > LOGIN_FLOW_TTL]
    for fid in expired:
        _login_flows.pop(fid, None)


@router.post("/login/start")
async def login_start(payload: dict = Body(default=None)):
    """发起 Z.AI OAuth，返回授权链接供前端展示。

    payload 可选 {"label": "acct-1"} —— 作为账号名前缀入池，便于多号识别。
    """
    payload = payload or {}
    label = (payload.get("label") or "").strip()[:32]
    _login_gc()
    flow = ZaiAuthFlow()
    try:
        flow_id, authorize_url = await flow.init()
    except Exception as err:  # noqa: BLE001
        logs.warn("oauth", f"登录初始化失败: {type(err).__name__}: {err}")
        raise HTTPException(502, f"登录初始化失败: {err}") from err
    _login_flows[flow_id] = {"flow": flow, "created": time.time(), "label": label}
    logs.info("oauth", f"发起登录 flow_id={flow_id} label={label or '-'}")
    return {
        "flow_id": flow_id,
        "authorize_url": authorize_url,
        "expires_in": int(LOGIN_FLOW_TTL),
    }


@router.get("/login/poll/{flow_id}")
async def login_poll(flow_id: str):
    """轮询授权状态；JWT 入池后立即 ready，API Key 兑换/刷新在后台回填。

    返回 status ∈ pending / ready / failed / expired。
    failed 附带 message（上游拒绝原因）；expired 表示会话超时需重新发起；
    未知 flow_id 一律 expired（而非 404），前端据此提示重新生成链接。
    官方 poll HTTP 4xx 视为终态 failed；5xx/网络抖动保持 pending 并打日志。
    """
    _login_gc()
    entry = _login_flows.get(flow_id)
    if not entry:
        return {"status": "expired"}
    flow = entry["flow"]
    try:
        data = await flow.poll(flow_id)
    except httpx.HTTPStatusError as err:
        code = err.response.status_code
        logs.warn("oauth", f"poll {flow_id} 上游 HTTP {code}")
        if 400 <= code < 500:
            _login_flows.pop(flow_id, None)
            return {"status": "failed", "message": f"上游拒绝轮询（HTTP {code}）"}
        return {"status": "pending"}
    except Exception as err:  # noqa: BLE001 - 单次网络抖动按 pending 处理
        logs.warn("oauth", f"poll {flow_id} 抖动: {type(err).__name__}")
        return {"status": "pending"}

    state = data.get("status")
    if state == "failed":
        _login_flows.pop(flow_id, None)
        reason = (data.get("message") or data.get("reason") or "授权失败或被拒绝")
        logs.warn("oauth", f"授权失败 flow_id={flow_id}: {reason}")
        return {"status": "failed", "message": str(reason)}
    if state != "ready":
        return {"status": "pending"}

    # 会话先摘除再入池：并发/重复 poll 不会再进入兑换链。
    _login_flows.pop(flow_id, None)

    # JWT 先入池并立刻 ready；兑换 API Key / 额度刷新改后台，避免卡住前端下一轮 poll。
    zcode_jwt = data.get("token")
    access_token = (data.get("zai") or {}).get("access_token")
    label = entry.get("label") or "oauth-login"
    account = None
    if zcode_jwt:
        account = store.add_account("zai", label, zcode_jwt)
    elif access_token:
        try:
            api_key = await asyncio.wait_for(
                flow.exchange_api_key(access_token), timeout=LOGIN_EXCHANGE_TIMEOUT
            )
            account = store.add_account("zai", label, api_key)
        except Exception as err:  # noqa: BLE001
            logs.warn("oauth", f"无 JWT 时兑换 API Key 失败: {err}")

    if account is None:
        logs.warn("oauth", f"授权结果无凭证 flow_id={flow_id}")
        return {"status": "failed", "message": "未能从授权结果中获取凭证"}

    if account.mode == "jwt":
        _schedule_auto_claim(account)  # 授权完成即激活+自动领取，入池即吃满活动
    _schedule_install(account)  # 按账号安装序（幂等；apiKey 账号同样安装）
    _schedule_login_followup(account, flow, access_token if zcode_jwt else None)
    logs.info("oauth", f"授权成功入池 {account.name} ({account.id}) mode={account.mode}")
    return {"status": "ready", "account": account.public_view()}


# ── 额度领取 ─────────────────────────────────────────────────────────────────
_auto_claim_tasks: set[asyncio.Task] = set()  # 强引用防 GC
_login_followup_tasks: set[asyncio.Task] = set()


def _schedule_login_followup(account, flow, access_token: str | None) -> None:
    """ready 后后台兑换 API Key 并刷新额度；失败只打日志，不影响已入池的 JWT。"""

    async def _job():
        live = store.find("zai", account.id)
        if live is None:
            return
        if access_token:
            try:
                api_key = await asyncio.wait_for(
                    flow.exchange_api_key(access_token), timeout=LOGIN_EXCHANGE_TIMEOUT
                )
                live = store.find("zai", account.id)
                if live is None:
                    return
                live.api_key = api_key
                store.update_account(live)
            except Exception as err:  # noqa: BLE001 - 兑换失败不影响 JWT 已入池
                logs.warn("oauth", f"账号 {account.name} 兑换 API Key 失败: {err}")
        live = store.find("zai", account.id)
        if live is None:
            return
        if live.mode == "jwt":
            try:
                await refresh_accounts([live])
            except Exception as err:  # noqa: BLE001
                logs.warn("oauth", f"账号 {account.name} 入池后额度刷新失败: {err}")

    task = asyncio.create_task(_job())
    _login_followup_tasks.add(task)
    task.add_done_callback(_login_followup_tasks.discard)


def _schedule_auto_claim(account) -> None:
    """入池后调度后台自动领取（激活上报 + 全量可领套餐）。

    不阻塞入池响应（验证码求解可长达数十秒）；仅 JWT 账号。失败不影响入池。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        return

    async def _job():
        live = store.find(account.provider, account.id)
        if live is None:
            return
        try:
            outcomes = await auto_claim_all_plans(live)
            live = store.find(account.provider, account.id)
            if live is None:
                return
            if outcomes:
                await refresh_accounts([live])  # 领到额度立即反映到 UI
        except Exception as err:  # noqa: BLE001 - 兜底：绝不冒泡
            logs.warn("claim", f"账号 {account.name} 自动领取任务异常: {err}")

    task = asyncio.create_task(_job())
    _auto_claim_tasks.add(task)
    task.add_done_callback(_auto_claim_tasks.discard)


def _jwt_accounts(account_ids: list[str] | None) -> list:
    accounts = store.list_accounts("zai")
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.id in wanted]
    return [a for a in accounts if a.mode == "jwt" and a.jwt_token]


# 按账号安装序的后台任务引用（同 _auto_claim_tasks：事件循环只持弱引用）
_install_tasks: set[asyncio.Task] = set()


def _schedule_install(account) -> None:
    """入池后调度后台按账号安装序（configs + 激活事件，幂等）。

    不阻塞入池响应；任何账号模式都跑（apiKey 账号 user_id 退空串，同官方
    未登录安装形态）。失败不影响入池；重复调用安全（installed_at 幂等跳过）。
    """

    async def _job():
        live = store.find(account.provider, account.id)
        if live is None:
            return
        try:
            await run_install_sequence_for_account(live)
        except Exception as err:  # noqa: BLE001 - 兜底：绝不冒泡
            logs.warn("install", f"账号 {account.name} 安装序任务异常: {err}")

    task = asyncio.create_task(_job())
    _install_tasks.add(task)
    task.add_done_callback(_install_tasks.discard)


@router.get("/claim/preview")
async def claim_preview(account_id: str | None = None):
    """立即拉取可领取套餐（全部/单个 JWT 账号）。

    先上报激活事件（zcode-switch claim_refresh 同形，模拟官方客户端当日活跃；
    疑似活动投放资格信号），上报失败不阻断 preview。
    """
    ids = [account_id] if account_id else None
    out = []
    for acc in _jwt_accounts(ids):
        blocked = billing_block_reason(acc, action="上游查询")
        if blocked:
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": [], "error": blocked,
                        "activated": False, "activation_error": None})
            continue
        try:
            activation_error = await report_activation_events(acc)
        except Exception as err:  # noqa: BLE001 - 上报失败不阻断 preview
            activation_error = str(err)
        try:
            plans = await preview_plans(acc)
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": plans, "error": None,
                        "activated": activation_error is None,
                        "activation_error": activation_error})
        except ClaimError as err:
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": [], "error": str(err),
                        "activated": activation_error is None,
                        "activation_error": activation_error})
    return {"preview": out}


@router.post("/claim")
async def claim(payload: dict = Body(default=None)):
    """领取套餐（body 可选 account_ids / plan_id）；缺省对全部 JWT 账号自动选最优套餐。

    返回 outcomes[]：{account_id, account_name, ok, plan_name?, grants?, message?}。
    """
    payload = payload or {}
    account_ids = payload.get("account_ids") or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    candidates = _jwt_accounts(account_ids)
    if not candidates:
        return {"outcomes": [], "summary": {"ok": 0, "fail": 0}}

    # 冷却 / 失效 JWT 不领取（billing/claim 是上游写流量，废票只会 401）
    outcomes = [
        {"account_id": a.id, "account_name": a.name, "ok": False,
         "message": billing_block_reason(a) or AUTH_EXPIRED_MESSAGE}
        for a in candidates if not a.allows_billing()
    ]
    for acc in candidates:
        if not acc.allows_billing():
            continue
        try:
            result = await do_claim(acc, plan_id)
        except ClaimError as err:
            logs.warn("claim", f"账号 {acc.name} 领取失败: {err}")
            outcomes.append({"account_id": acc.id, "account_name": acc.name,
                             "ok": False, "message": str(err)})
            continue
        except CaptchaSolveError as err:
            # 验证码求解失败（get_verify_param）：明确业务回执而非裸 500
            logs.err("claim", f"账号 {acc.name} 领取失败: {err}")
            outcomes.append({"account_id": acc.id, "account_name": acc.name,
                             "ok": False, "message": str(err)})
            continue
        except RuntimeError as err:
            # 兜底：captcha 层历史语义的运行时故障，防回归裸 500
            logs.err("claim", f"账号 {acc.name} 领取失败: {err}")
            outcomes.append({"account_id": acc.id, "account_name": acc.name,
                             "ok": False, "message": str(err)})
            continue
        await refresh_accounts([acc])
        outcomes.append({"account_id": acc.id, "account_name": acc.name,
                         "ok": True, **result})
    ok = sum(1 for o in outcomes if o["ok"])
    return {"outcomes": outcomes, "summary": {"ok": ok, "fail": len(outcomes) - ok}}


@router.get("/claim/captcha-config")
async def claim_captcha_config():
    """手动领取用：阿里验证码 SDK 初始化参数（前端浏览器内完成人机验证）。"""
    from ..captcha import captcha_manager

    config = await captcha_manager.fetch_config()
    return {
        "enabled": bool(config.get("enabled", True)),
        "scene_id": config.get("sceneId") or "",
        "region": config.get("region") or "",
        "prefix": config.get("prefix") or "",
    }


@router.post("/claim/manual")
async def claim_manual(payload: dict = Body(...)):
    """手动领取：body {account_id, captcha_verify_param, captcha_region?, plan_id?}。

    verify_param 必须来自用户浏览器内阿里 SDK 滑块成功回调（无头环境无法求解）。
    """
    account_id = (payload.get("account_id") or "").strip()
    verify_param = (payload.get("captcha_verify_param") or "").strip()
    region = (payload.get("captcha_region") or "").strip() or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    if not account_id:
        raise HTTPException(400, "缺少 account_id")

    acc = store.find("zai", account_id)
    if not acc or acc.mode != "jwt" or not acc.jwt_token:
        raise HTTPException(404, "JWT 账号不存在")
    blocked = billing_block_reason(acc)
    if blocked:
        return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                              "ok": False, "message": blocked}],
                "summary": {"ok": 0, "fail": 1}}

    try:
        result = await claim_with_captcha(acc, verify_param, region, plan_id)
    except ClaimError as err:
        return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                              "ok": False, "message": str(err)}],
                "summary": {"ok": 0, "fail": 1}}
    await refresh_accounts([acc])
    return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                          "ok": True, **result}],
            "summary": {"ok": 1, "fail": 0}}


# ── 设置 ─────────────────────────────────────────────────────────────────────
def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


@router.get("/settings")
async def get_settings():
    from .. import settings as app_settings

    admin_key = store.admin_key()
    gateway_key = store.gateway_key()
    return {
        "admin_key_set": bool(admin_key),
        "admin_key_masked": _mask_secret(admin_key),
        "admin_key_is_default": bool(admin_key) and admin_key == app_settings.DEFAULT_ADMIN_KEY,
        "gateway_key_set": bool(gateway_key),
        "gateway_key_masked": _mask_secret(gateway_key),
        "quota_refresh_interval": store.quota_refresh_interval(),
        "account_concurrency": store.account_concurrency(),
    }


@router.put("/settings")
async def update_settings(payload: dict = Body(...)):
    if "admin_key" in payload:
        key = (payload["admin_key"] or "").strip()
        if not key:
            raise HTTPException(400, "后台密钥不能为空")
        if "…" in key or key == "••••":
            pass  # 前端回填的掩码，不改密
        else:
            store.set_setting("admin_key", key)
    if "gateway_key" in payload:
        key = (payload["gateway_key"] or "").strip()
        if "…" in key or key == "••••":
            pass
        else:
            store.set_setting("gateway_key", key)
    if "quota_refresh_interval" in payload:
        try:
            interval = max(0, int(payload["quota_refresh_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "刷新间隔必须是非负整数") from None
        store.set_setting("quota_refresh_interval", str(interval))
    if "account_concurrency" in payload:
        try:
            concurrency = max(0, int(payload["account_concurrency"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "账号并发必须是非负整数（0 = 不限）") from None
        store.set_setting("account_concurrency", str(concurrency))
    return {"ok": True}


# ── 导入 / 导出 ─────────────────────────────────────────────────────────────
@router.get("/export")
async def export_accounts():
    return store.export()


@router.post("/import")
async def import_accounts(payload: dict = Body(...)):
    existing = {a.id for a in store.list_accounts()}
    count = store.import_accounts(payload)
    # 导入的新账号：安装序（幂等）+ JWT 账号激活+自动领取（幂等：重复 token 不新增）
    imported = [a for a in store.list_accounts("zai") if a.id not in existing]
    for acc in imported:
        _schedule_install(acc)
        if acc.mode == "jwt":
            _schedule_auto_claim(acc)
    return {"count": count}


# ── 请求监控 ─────────────────────────────────────────────────────────────────
@router.get("/monitoring")
async def monitoring():
    """网关请求环形日志（内存态，重启清零）。前端自行聚合统计。"""
    return {"entries": reqlog.snapshot(), "keep": reqlog.KEEP}


@router.post("/monitoring/clear")
async def monitoring_clear():
    reqlog.clear()
    return {"ok": True}
