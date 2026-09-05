"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx

from . import constants, logs, settings
from .models import Account, Status
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
    分离）；platform/os 伪装复用 messages 同一套常量；每请求全新 x-request-id。
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}",
        "HTTP-Referer": constants.ZCODE_ORIGIN,
        "X-Title": constants.BILLING_TITLE,
        "X-ZCode-App-Version": constants.BILLING_APP_VERSION,
        "X-Platform": constants.CLIENT_PLATFORM,
        "X-Release-Channel": constants.BILLING_RELEASE_CHANNEL,
        "X-Client-Language": constants.IDENTITY_CLIENT_LANGUAGE,
        "X-Client-Timezone": constants.IDENTITY_CLIENT_TIMEZONE,
        "X-Os-Category": constants.IDENTITY_OS_CATEGORY,
        "X-Os-Version": constants.IDENTITY_OS_VERSION,
        "X-Device-Mid": device_mid(),
        "x-request-id": str(uuid.uuid4()),
    }
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
    return headers


def _bonus_active(plan: dict, now: float) -> bool:
    """plan 是否带有已生效的一次性赠送授权（balance 不含这类额度）。

    用于额度耗尽判定：日窗口用完但赠送池有效时，账号仍有真实可用额度。
    """
    for e in (plan or {}).get("entitlements") or []:
        if e.get("period") != "one_time":
            continue
        eff = e.get("effective_at") or 0
        ends = e.get("ends_at") or e.get("expires_at") or 0
        if eff and eff <= now and (not ends or now <= ends):
            return True
    return False


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
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

    now = time.time()
    account.last_checked_at = now

    # 鉴权失败 → 标记 invalid
    if billing_res is not None and billing_res.status_code in (401, 403):
        body = (billing_res.text or "").lower()
        if "captcha" not in body and "verify" not in body:
            account.status = Status.INVALID
            account.last_error = f"鉴权失败 HTTP {billing_res.status_code}"
            store.update_account(account)
            return {"error": account.last_error}

    if billing_res is not None and billing_res.status_code == 200:
        try:
            data = billing_res.json()
            result["billing"] = data
            plans = (data.get("data") or {}).get("plans") or []
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
            for bal in (data.get("data") or {}).get("balances") or []:
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
                    prev["expires_at"] = max(prev.get("expires_at") or 0, window.get("expires_at") or 0)
                    logs.warn("quota", f"balance 同名窗口 {name} 已合并")
                else:
                    quota_map[name] = window
        except (ValueError, KeyError):
            pass

    if usage_res is not None and usage_res.status_code == 200:
        try:
            account.usage = usage_res.json().get("data") or {}
            result["usage"] = account.usage
        except (ValueError, KeyError):
            pass

    if quota_map:
        account.quota = quota_map
        # 额度耗尽判定：所有日窗口剩余 <= 0。注意 balance 不含一次性赠送池——
        # 赠送池有效时不判耗尽，否则账号会被路由跳过而实际仍有 3 亿级可用额度
        now = time.time()
        remainings = [
            q.get("remaining") for q in quota_map.values() if q.get("remaining") is not None
        ]
        daily_exhausted = bool(remainings) and all((r or 0) <= 0 for r in remainings)
        has_bonus = _bonus_active(account.plan, now) or any(
            _bonus_active(p, now) for p in account.plans
        )
        has_daily = bool(remainings) and any((r or 0) > 0 for r in remainings)
        if daily_exhausted and not has_bonus:
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完"
        elif account.status in (Status.EXHAUSTED, Status.COOLING, Status.INVALID) and (
            has_daily or has_bonus
        ):
            # 额度恢复（窗口重置 / 赠送池生效）→ 重新激活。风控冷却例外：冷却期内
            # 不该有 billing 流量（monitor 已跳过），此处兜底不再提前解除
            if not account.is_cooling():
                account.status = Status.ACTIVE
                account.last_error = None
                account.cooling_until = None

    store.update_account(account)
    return result or {"error": "无法获取额度数据"}


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
                        if a.mode == "jwt" and a.status != Status.DISABLED
                        and not a.is_cooling()
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
