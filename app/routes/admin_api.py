"""后台管理 API：/admin/api/*（账号池、设置、用量监控）。"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import logs, reqlog
from ..auth_admin import verify_admin_key
from ..captcha import CaptchaSolveError
from ..claim import (
    ClaimError,
    auto_claim_all_plans,
    claim_with_captcha,
    preview_plans,
    report_activation_events,
)
from ..claim import claim as do_claim
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
    for acc in fresh:
        if acc.id not in existing:
            _schedule_auto_claim(acc)
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
        acc.mode = "jwt" if (secret.count(".") == 2 and acc.provider == "zai") else "apiKey"
        acc.jwt_token = secret if acc.mode == "jwt" else None
        acc.api_key = None if acc.mode == "jwt" else secret
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
    store.update_account(acc)
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
    # 冷却中账号不打 billing（与 QuotaMonitor 同一不变量：冷却期零上游流量）
    targets = [a for a in pool if not a.is_cooling()]
    summary = await refresh_accounts(targets)
    return {
        "summary": summary,
        "count": len(targets),
        "skipped_cooling": len(pool) - len(targets),
    }


@router.post("/accounts/{account_id}/refresh")
async def refresh_one(account_id: str):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.mode != "jwt":
        return {"ok": False, "message": "仅 Coding Plan (JWT) 账号支持额度查询"}
    if acc.is_cooling():
        return {"ok": False, "message": "账号冷却中（风控/限流），已跳过上游刷新",
                "account": acc.public_view()}
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
        raise HTTPException(502, f"登录初始化失败: {err}") from err
    _login_flows[flow_id] = {"flow": flow, "created": time.time(), "label": label}
    return {
        "flow_id": flow_id,
        "authorize_url": authorize_url,
        "expires_in": int(LOGIN_FLOW_TTL),
    }


@router.get("/login/poll/{flow_id}")
async def login_poll(flow_id: str):
    """轮询授权状态；成功后自动兑换凭证并加入账号池。

    返回 status ∈ pending / ready / failed / expired。
    failed 附带 message（上游拒绝原因）；expired 表示会话超时需重新发起；
    未知 flow_id 一律 expired（而非 404），前端据此提示重新生成链接。
    """
    _login_gc()
    entry = _login_flows.get(flow_id)
    if not entry:
        return {"status": "expired"}
    flow = entry["flow"]
    try:
        data = await flow.poll(flow_id)
    except Exception:  # noqa: BLE001 - 单次网络抖动按 pending 处理
        return {"status": "pending"}

    state = data.get("status")
    if state == "failed":
        _login_flows.pop(flow_id, None)
        reason = (data.get("message") or data.get("reason") or "授权失败或被拒绝")
        return {"status": "failed", "message": str(reason)}
    if state != "ready":
        return {"status": "pending"}

    # 会话先摘除再兑换：并发/重复 poll 不会再进入兑换链，
    # 也不会在长时间内联操作期间让前端轮询叠加出第二份上游调用。
    _login_flows.pop(flow_id, None)

    # 授权成功：保存 Coding Plan JWT，并尝试兑换 API Key 作为同账号回退
    zcode_jwt = data.get("token")
    access_token = (data.get("zai") or {}).get("access_token")
    label = entry.get("label") or "oauth-login"
    account = None
    if zcode_jwt:
        account = store.add_account("zai", label, zcode_jwt)
    if access_token:
        try:
            api_key = await asyncio.wait_for(
                flow.exchange_api_key(access_token), timeout=LOGIN_EXCHANGE_TIMEOUT
            )
            if account is not None:
                account.api_key = api_key
                store.update_account(account)
            else:
                account = store.add_account("zai", label, api_key)
        except Exception:  # noqa: BLE001 - 兑换失败不影响 JWT 已入池
            pass

    if account is None:
        return {"status": "failed", "message": "未能从授权结果中获取凭证"}

    if account.mode == "jwt":
        await refresh_accounts([account])
        _schedule_auto_claim(account)  # 授权完成即激活+自动领取，入池即吃满活动
    return {"status": "ready", "account": account.public_view()}


# ── 额度领取 ─────────────────────────────────────────────────────────────────
_auto_claim_tasks: set[asyncio.Task] = set()  # 强引用防 GC


def _schedule_auto_claim(account) -> None:
    """入池后调度后台自动领取（激活上报 + 全量可领套餐）。

    不阻塞入池响应（验证码求解可长达数十秒）；仅 JWT 账号。失败不影响入池。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        return

    async def _job():
        try:
            outcomes = await auto_claim_all_plans(account)
            if outcomes:
                await refresh_accounts([account])  # 领到额度立即反映到 UI
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


@router.get("/claim/preview")
async def claim_preview(account_id: str | None = None):
    """立即拉取可领取套餐（全部/单个 JWT 账号）。

    先上报激活事件（zcode-switch claim_refresh 同形，模拟官方客户端当日活跃；
    疑似活动投放资格信号），上报失败不阻断 preview。
    """
    ids = [account_id] if account_id else None
    out = []
    for acc in _jwt_accounts(ids):
        if acc.is_cooling():
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": [], "error": "账号冷却中（风控/限流），已跳过上游查询",
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

    # 冷却中账号不领取（billing/claim 是上游写流量，风控期打上去只会加剧）
    outcomes = [
        {"account_id": a.id, "account_name": a.name, "ok": False,
         "message": "账号冷却中（风控/限流），已跳过领取"}
        for a in candidates if a.is_cooling()
    ]
    for acc in candidates:
        if acc.is_cooling():
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
    if acc.is_cooling():
        return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                              "ok": False, "message": "账号冷却中（风控/限流），已跳过领取"}],
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
@router.get("/settings")
async def get_settings():
    return {
        "admin_key": store.admin_key(),
        "gateway_key": store.gateway_key(),
        "quota_refresh_interval": store.quota_refresh_interval(),
    }


@router.put("/settings")
async def update_settings(payload: dict = Body(...)):
    if "admin_key" in payload:
        key = (payload["admin_key"] or "").strip()
        if not key:
            raise HTTPException(400, "后台密钥不能为空")
        store.set_setting("admin_key", key)
    if "gateway_key" in payload:
        store.set_setting("gateway_key", (payload["gateway_key"] or "").strip())
    if "quota_refresh_interval" in payload:
        try:
            interval = max(0, int(payload["quota_refresh_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "刷新间隔必须是非负整数") from None
        store.set_setting("quota_refresh_interval", str(interval))
    return {"ok": True}


# ── 导入 / 导出 ─────────────────────────────────────────────────────────────
@router.get("/export")
async def export_accounts():
    return store.export()


@router.post("/import")
async def import_accounts(payload: dict = Body(...)):
    existing = {a.id for a in store.list_accounts("zai")}
    count = store.import_accounts(payload)
    # 导入的 JWT 账号同样入池即激活+自动领取（幂等：重复 token 不会新增）
    imported = [a for a in store.list_accounts("zai")
                if a.id not in existing and a.mode == "jwt"]
    for acc in imported:
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
