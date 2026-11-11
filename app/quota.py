"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx

from . import business_error, constants, logs, settings
from .gate import gate

# 一次性赠送授权判定收敛到 models（2026-11-12 review #2）：同一上游结构的
# 同一判定不允许两份实现漂移——models 版经 expires_epoch 归一（兼容 ISO8601/
# 字符串 epoch），原本地实现只做裸数字比较，遇到非 epoch 形态会静默失效。
from .models import Account, Status, expires_epoch, window_expired, window_usable
from .models import _plan_bonus_active as _bonus_active
from .store import store

_DEVICE_MID: str | None = None


def device_mid() -> str:
    """本机设备 ID（ZCode 客户端 telemetry 同款语义）。

    billing 全家桶（current/balance/usage/preview/claim）必需 X-Device-Mid，
    缺失时上游返回 code=3001 parameter error。首次生成后持久化到 data 目录。
    """
    global _DEVICE_MID
    if _DEVICE_MID:
        return _DEVICE_MID
    path = settings.DATA_DIR / "device_mid"
    try:
        _DEVICE_MID = path.read_text().strip()
        if _DEVICE_MID:
            return _DEVICE_MID
    except OSError:
        pass
    _DEVICE_MID = str(uuid.uuid4())
    try:
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        with open(path, "x") as f:
            f.write(_DEVICE_MID)
    except OSError as err:  # noqa: BLE001 - 生成失败不阻断查询，仅进程内复用
        logs.warn("quota", f"device_mid 持久化失败（仅进程内生效）: {err}")
    return _DEVICE_MID


def _auth_headers(account: Account) -> dict:
    """billing 族请求头（对齐 zcode-switch zai_billing_headers 实证形态）。

    UA/版本走 BILLING_APP_VERSION（官方桌面端现行版，与 messages 指纹刻意
    分离）；平台/os/语言/时区/设备 ID 按账号指纹档案出值（每账号独立设备，
    见 fingerprint.py）；每请求全新 x-request-id。
    """
    from .fingerprint import profile_for

    profile = profile_for(account)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}",
        "HTTP-Referer": constants.ZCODE_ORIGIN,
        "X-Title": constants.BILLING_TITLE,
        "X-ZCode-App-Version": constants.BILLING_APP_VERSION,
        "X-Platform": profile.platform_full,
        "X-Release-Channel": constants.BILLING_RELEASE_CHANNEL,
        "X-Client-Language": profile.language,
        "X-Client-Timezone": profile.timezone,
        "X-Os-Category": profile.os_category,
        "X-Os-Version": profile.os_version,
        "X-Device-Mid": profile.device_mid,
        "x-request-id": str(uuid.uuid4()),
    }
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
    return headers


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
    live = store.find(account.provider, account.id)
    if live is None:
        return {"error": "账号已删除"}
    if live is not account:
        account = live
    if not account.allows_billing():
        # 与 admin/monitor 同一扇门。只有无 JWT / JWT 失效（INVALID）才拦：
        # 停用、风控禁用、冷却中照常查询（领取与使用分离，2026-11-04 口径）。
        from .claim import AUTH_EXPIRED_MESSAGE, billing_block_reason

        return {
            "error": account.last_error
            or billing_block_reason(account, action="上游查询")
            or AUTH_EXPIRED_MESSAGE
        }
    headers = _auth_headers(account)
    base = settings.ZCODE_BILLING_BASE
    result: dict = {}

    async with httpx.AsyncClient(timeout=20) as client:
        async def _get(path: str):
            try:
                return await client.get(f"{base}{path}", headers=headers)
            except httpx.HTTPError:
                return None

        billing_res, balance_res, usage_res = await asyncio.gather(
            _get("/billing/current"),
            _get("/billing/balance"),
            _get("/usage"),
        )

    live = store.find(account.provider, account.id)
    if live is None:
        return {"error": "账号已删除"}
    if live is not account:
        account = live

    now = time.time()
    account.last_checked_at = now

    # 鉴权失败 → 标记 invalid
    if billing_res is not None and billing_res.status_code in (401, 403):
        body = (billing_res.text or "").lower()
        if "captcha" not in body and "verify" not in body:
            from .claim import AUTH_EXPIRED_MESSAGE

            account.status = Status.INVALID
            account.last_error = AUTH_EXPIRED_MESSAGE
            store.update_account(account)
            return {"error": account.last_error}

    # HTTP 200 承载的业务错误（Plan 通道实测：额度/风控类拒绝常是 200 +
    # {"code":1005,...}）。三条 billing 请求各自判：命中业务错误的**不得写库**，
    # 否则会把 quota 快照 / plans 洗空，看起来像「上游没投放、额度未知」。
    errors: dict[str, int] = {}
    billing_plans: list | None = None  # 本次 billing/current 实际解析到的 plans

    if billing_res is not None and billing_res.status_code == 200:
        try:
            data = billing_res.json()
            result["billing"] = data
            code = business_error.business_code(data)
            if code is not None:
                errors["current"] = code
            else:
                plans = (data.get("data") or {}).get("plans") or []
                billing_plans = plans
                # 全量保留（多套餐时 entitlements 不丢）；plan 兼容保留首个
                account.plans = plans
                account.plan = plans[0] if plans else {}
        except (ValueError, KeyError):
            pass

    quota_map: dict = {}
    if balance_res is not None and balance_res.status_code == 200:
        try:
            data = balance_res.json()
            result["balance"] = data
            code = business_error.business_code(data)
            if code is not None:
                errors["balance"] = code
            else:
                # balance 也带 plans[]（含 plan_id）。`/billing/current` 偶发失败时
                # 这里就是套餐类型的唯一来源——少了它 trial 号会退化成「未知套餐」，
                # 拿不到体验档的并发收紧（见 models.Account.plan_kind）。
                bal_plans = (data.get("data") or {}).get("plans") or []
                if bal_plans and not account.plans:
                    account.plans = bal_plans
                    account.plan = bal_plans[0]
                # 注意：quota_verified_at **不在这里**打点。balance 200 但 balances
                # 为空（新号 billing 全空形态，见下方注释）时什么也没确认——此时
                # 打点会把旧的过期快照误升格为「过期后实查确认」，正好把 2.7.8
                # 要救的号从另一扇门判死。打点在下方 quota_map 非空分支。
            for bal in [b for b in (data.get("data") or {}).get("balances") or []
                        if code is None]:
                name = bal.get("show_name") or bal.get("model") or "model"
                window = {
                    "total": bal.get("total_units"),
                    "used": bal.get("used_units"),
                    "remaining": bal.get("remaining_units"),
                    "expires_at": bal.get("expires_at"),
                }
                prev = quota_map.get(name)
                if prev:
                    # 防御：同模型多窗口（如日窗 + 一次性）合并，避免后者覆盖前者
                    for k in ("total", "used", "remaining"):
                        prev[k] = (prev.get(k) or 0) + (window.get(k) or 0)
                    # expires_at 保留**更晚**的那个。该字段线上是 epoch 秒、
                    # 快照里也出现过 ISO8601 字符串，直接 max() 会按字典序比
                    # （str 与 int 混型甚至 TypeError），必须先归一成 epoch 再挑。
                    prev_end = expires_epoch(prev.get("expires_at"))
                    new_end = expires_epoch(window.get("expires_at"))
                    if new_end is not None and (prev_end is None or new_end > prev_end):
                        prev["expires_at"] = window.get("expires_at")
                    logs.warn("quota", f"balance 同名窗口 {name} 已合并")
                else:
                    quota_map[name] = window
        except (ValueError, KeyError):
            pass

    if usage_res is not None and usage_res.status_code == 200:
        try:
            data = usage_res.json()
            code = business_error.business_code(data)
            if code is not None:
                errors["usage"] = code
            else:
                account.usage = data.get("data") or {}
                result["usage"] = account.usage
        except (ValueError, KeyError):
            pass

    if quota_map:
        account.quota = quota_map
        # 快照真实写库的时刻 = 「过期后实查」的证据点（见 models.Account.quota_expired）。
        # 绑在 quota_map 非空上：只有真的取到窗口，才谈得上「确认」它们的状态。
        account.quota_verified_at = now
        # 额度耗尽判定：逐窗口算「有效余量」（remaining > 0 且未过期）。
        # 原实现只看 remaining <= 0，把**已过期窗口**当可用 —— 2026-10-31 实证：
        # 老号 GLM-5.3-Flash 本地 remaining=1e8、expires_at 在 28 天前，上游对
        # 每个 messages 一律回 {"code":1005}。只看余额会让这类号长期占着轮询位。
        # 注意 balance 不含一次性赠送池：赠送池有效时不判耗尽。
        now = time.time()
        usable = [w for w in quota_map.values() if window_usable(w, now)]
        all_expired = all(window_expired(w, now) for w in quota_map.values())
        has_bonus = _bonus_active(account.plan, now) or any(
            _bonus_active(p, now) for p in account.plans
        )
        has_daily = bool(usable)
        # 风控禁用 / 废 JWT：billing 数据照写（领取链要用），但**绝不**用额度数字改
        # 状态——否则 DISABLED→EXHAUSTED→（额度恢复）→ACTIVE 会绕过人工/风控封禁。
        if account.status in (Status.INVALID, Status.DISABLED):
            pass
        elif not has_daily and not has_bonus:
            account.status = Status.EXHAUSTED
            account.last_error = "额度已过期" if all_expired else "额度已用完"
        elif account.status in (Status.EXHAUSTED, Status.COOLING) and (
            has_daily or has_bonus
        ):
            # 额度恢复（窗口重置 / 赠送池生效）→ 重新激活。冷却期内不提前解除；
            # 废 JWT / 风控禁用绝不能因额度数字复活 Plan 通道。
            if not account.is_cooling():
                account.status = Status.ACTIVE
                account.last_error = None
                account.cooling_until = None
        # 套餐窗口闸门解除：任一号实查到可用窗口（哪怕它本来状态就健康，如
        # hold 期间未被消息请求碰过的号），全池「确认枯竭」的前提即被推翻。
        # 放在状态机之外——闸门是进程级状态，不与单账号状态耦合（2026-11-12）。
        if has_daily or has_bonus:
            gate.clear_pool_hold()
            # 模型级额度耗尽冷却整号清除（2.7.29）：billing 实查拿到该号账本，
            # 「（账号，模型）已确认 1005」的前提被推翻——避免冷却记忆比事实
            # 活得久（TTL 是兜底，这里是主动解除）。
            from .plan_guard import clear_account as _pg_clear
            _pg_clear(account.id)
    elif errors:
        # billing 回了业务错误、但没拿到任何窗口：**不据此判死**。
        # 上游对「体验套餐/新号」的 billing 支持并不一致（老实证：billing 全空
        # 仍有 100% 可用的试用额度），拿不到账面数据 ≠ 没额度。真正的判定只有
        # 两处：balance 窗口全部失效（上面分支）、messages 真实回 1005（gateway）。
        # 这里只留痕，让一次真实请求去定生死——否则新号会被永久挡在门外。
        detail = "，".join(f"{k}=code{v}" for k, v in errors.items())
        logs.warn("quota", f"账号 {account.name} billing 业务错误但保留可选（{detail}）")

    # 新号体验额度：上游对新注册账号可能 **billing 全空/全拒**，但 messages 仍能
    # 正常服务（2026-09-06 实测：空 billing 的 oauth-login 100% 成功服务请求；
    # 2026-11-01 复核：billing/preview、balance 均空/1005，新号照样有体验套餐）。
    # 只凭 billing 就判死会把真能用的号挡在门外。这里**只给 ACTIVE 且零成功请求
    # 的号**一条提示，绝不复活已判死的号——否则「首探 1005 → 判死 → 下次刷新又
    # 拉活 → 再撞 1005」会无限循环，每圈烧一次真实请求（比误判更贵）。
    elif not (quota_map or billing_plans or result.get("usage")):
        if account.use_count == 0 and account.status == Status.ACTIVE:
            account.last_error = "billing 为空，待首次请求验证（疑似新号体验额度）"

    store.update_account(account)
    # 「三路全业务错误」才整体报失败（调用方据此区分「上游拒绝」与「上游确实
    # 什么都没有」）。一路成功就照常返回，错误只记日志——避免把「部分可用」
    # 误报成刷新失败。
    if errors and "current" in errors and not quota_map and not result.get("usage"):
        detail = "，".join(f"{k}=code{v}" for k, v in errors.items())
        logs.warn("quota", f"账号 {account.name} billing 全业务错误（{detail}）")
        return {"error": f"上游业务错误（{detail}）"}
    return result or {"error": "无法获取额度数据"}


def _normalize_limits(data: dict) -> list[dict]:
    """`data.limits[]` → 紧凑快照（TOKENS_LIMIT / TIME_LIMIT 两类窗口混排）。"""
    out: list[dict] = []
    for item in (data.get("data") or {}).get("limits") or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "type": item.get("type"),
            "unit": item.get("unit"),
            "number": item.get("number"),
            "usage": item.get("usage"),
            "remaining": item.get("remaining"),
            "percentage": item.get("percentage"),
            "next_reset_time": item.get("next_reset_time"),
        })
    return out


async def fetch_coding_limits(account: Account) -> dict:
    """拉取 Coding 平面额度（api.z.ai monitor 端点）。

    与 `fetch_quota` 是**两套独立账本**：Plan 通道额度来自 zcode.z.ai 的
    billing 族；Coding 平面（API Key 直连 api.z.ai）的额度完全不在那套账里。
    体验套餐号在 billing 侧常全空/全拒，但这里能给出真实上限表——作为第二
    额度源补盲，只读、不写账号状态（避免与 billing 判定打架）。

    返回 {"limits": [...], "raw": {...}} 或 {"error": ...}
    """
    api_key = (account.api_key or "").strip()
    if not api_key:
        return {"error": "该账号没有 API Key，无法查询 Coding 平面额度"}
    headers = {"authorization": api_key, "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(constants.CODING_LIMIT_URL, headers=headers)
    except httpx.HTTPError as err:
        return {"error": f"请求上游失败: {err}"}
    try:
        data = resp.json()
    except ValueError:
        return {"error": f"上游返回非 JSON（HTTP {resp.status_code}）"}
    if not isinstance(data, dict):
        return {"error": "上游返回结构异常"}
    code = business_error.business_code(data)
    if code is not None:
        return {"error": str(data.get("msg") or f"业务错误 code={code}"), "code": code}
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}"}
    return {"limits": _normalize_limits(data), "raw": data}


async def refresh_accounts(accounts: list[Account]) -> dict:
    """并发刷新一批账号，返回汇总。"""
    if not accounts:
        return {"ok": 0, "fail": 0}
    sem = asyncio.Semaphore(8)

    async def _one(acc: Account) -> bool:
        async with sem:
            res = await fetch_quota(acc)
            return "error" not in res

    results = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    return {"ok": ok, "fail": len(accounts) - ok}


class QuotaMonitor:
    """后台周期性刷新可管理账号的额度，实现实时用量监控。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # 启动后先等几秒，避免与服务启动争抢
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.quota_refresh_interval()  # 实时读取设置，改后即生效
            if interval > 0:
                try:
                    accounts = [
                        a for a in store.list_accounts("zai")
                        if a.mode == "jwt" and a.allows_billing()
                    ]
                    if accounts:
                        await refresh_accounts(accounts)
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("quota", f"后台刷新出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = QuotaMonitor()
