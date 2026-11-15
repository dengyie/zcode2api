"""TetherPool 出口供给管理器（per-account Mode B，2.7.39）。

supply=tetherpool 时：每个 hub 账号绑定一个 TetherPool allocation
（TP account_id = `zh-<hub账号id>`，互斥由 exclude_node=其它账号节点强制），
数据面 = `:9101` 统一网关（网关 URL 确定性构造见 egress.tetherpool_gateway_url——
TP 侧 rebalance 只改服务端绑定，URL 恒定，hub 代理对象零重建）。

职责：
- rebalance：边缘封禁（405 HTML，ban 旧节点 + force_rebalance + 即时探针验证）
  / 连接失败聚类（节点不可达，不 ban 只换）/ 健康循环探针失败
- 绑定引导：egress_node 为空的账号懒 acquire（幂等，TP 粘性）
- 后台健康循环：逐绑定账号 ip-api 轻探针，失败 → rebalance

失败语义：TP 不可达 / pool_exhausted / 探针全败 → 保持旧绑定 + ALERT（绝不
本地降级直连机房 IP）。每账号爆炸半径 1/N（per-account 隔离）。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import egress, settings
from .models import PROVIDERS

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROBE_BODY = json.dumps({"model": "GLM-5.3-Flash", "max_tokens": 16,
                          "messages": [{"role": "user", "content": "hi"}]})
_PROBE_HEADERS = {"Content-Type": "application/json", "User-Agent": "ZCode/3.17.2"}


def _tp_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    try:
        with open(settings.TP_TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            headers["Authorization"] = "Bearer " + token
    except OSError:
        pass
    return headers


def _tp_http(method: str, url: str, body=None, headers=None, timeout: float = 15):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with _NOPROXY.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _opener(proxy: str | None):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def gateway_url_for(account) -> str:
    """per-account 网关代理 URL（确定性构造；rebalance 不改变它）。"""
    return egress.tetherpool_gateway_url(account)


def _other_nodes(account) -> list[str]:
    """其它绑定账号的 TP 节点名（互斥 exclude_node 集合，风控核心：
    任何时刻不允许两个账号共用一个节点）。扫全 provider（add_account 给任何
    命中灰度名单的账号发网关 URL，bigmodel 账号节点同样占位——review 2026-11-15）。"""
    from .store import store

    out = []
    for provider in PROVIDERS:
        for a in store.list_accounts(provider):
            if a.id == account.id:
                continue
            node = getattr(a, "egress_node", None)
            if node:
                out.append(node)
    return out


async def tp_acquire(account, force_rebalance: bool = False,
                     extra_exclude: list[str] | None = None) -> dict | None:
    """acquire 一个 allocation（粘性；force_rebalance 强制换）。失败 → None。"""
    params = {
        "service": settings.TP_SERVICE,
        "account_id": "zh-" + str(account.id),
        "region": settings.TP_REGION,
        "type": settings.TP_TYPE,
        "region_strict": "true",
        "avoid_banned": "true",
        "avoid_same_isp": "true",
    }
    if force_rebalance:
        params["force_rebalance"] = "true"
    exclude = list(extra_exclude or [])
    if exclude:
        params["exclude_node"] = ",".join(exclude)
    query = urllib.parse.urlencode(params)
    st, raw = await asyncio.to_thread(
        _tp_http, "POST", f"{settings.TP_API}/v1/allocation/acquire?{query}",
        None, _tp_headers(), 20)
    if st != 200:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def tp_ban(node_name: str, reason: str, evidence: dict,
                 account_name: str) -> None:
    from . import logs
    body = {"service": settings.TP_SERVICE, "account": account_name,
            "node_name": node_name, "reason": reason,
            "evidence": {k: str(v)[:120] for k, v in (evidence or {}).items()}}
    st, raw = await asyncio.to_thread(
        _tp_http, "POST", f"{settings.TP_API}/v1/feedback/ban",
        json.dumps(body), _tp_headers(), 15)
    if st in (200, 201, 202):
        logs.info("egress_pool", f"ban({node_name}) 已隔离: {raw.strip()[:100]}")
    else:
        logs.warn("egress_pool", f"ban({node_name}) 失败 HTTP {st}: {raw.strip()[:120]}")


def _edge_kind(raw: str) -> str:
    low = raw.lstrip().lower()
    if low.startswith("<!doctype") or "<html" in low[:200] or "<title>405" in low:
        return "HTML-EDGE"
    return "JSON-APP"


class TetherPoolEgress:
    """per-account TetherPool 出口管理器（单例 egress_pool）。"""

    def __init__(self) -> None:
        self._loop_task: asyncio.Task | None = None
        self._rebalance_at: dict[str, float] = {}  # account_id → monotonic 去抖
        self._inflight: set[asyncio.Task] = set()  # 健康检查任务强引用（防 GC）
        self._locks: dict[str, asyncio.Lock] = {}  # account_id → rebalance 互斥
                                                 # （TOCTOU 收口：同账号串行，
                                                 #   跨账号仍可并发——互斥由
                                                 #   exclude_node 保证）
        self._probe_fail_streak: dict[str, int] = {}  # account_id → 连续探针失败轮数

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def clear_probe_streak(self, account_id: str) -> None:
        """清该账号的探针失败计数（换绑提交 / 账号禁用时调用——旧账不得
        背到新节点或重新启用后的账上）。"""
        self._probe_fail_streak.pop(account_id, None)

    # ── 绑定/换节点编排 ────────────────────────────────────────────────────
    async def ensure_binding(self, account) -> bool:
        """懒引导：egress_node 为空的绑定账号 acquire 一次（幂等，TP 粘性）。

        必须带 exclude_node=其它账号节点（风控核心）：首次分配正是节点归属
        发生的时刻，不带互斥 = 新账号可静默落到已属其它账号的节点。"""
        if account.egress_node:
            return True
        d = await tp_acquire(account, extra_exclude=_other_nodes(account))
        if not d:
            return False
        account.egress_node = d.get("node_name")
        from .store import store
        store.update_account(account)
        return bool(account.egress_node)

    async def rebalance(self, account, reason: str, ban: bool = True) -> bool:
        """换该账号的 TP 绑定节点：可选 ban 旧节点 → force_rebalance（最多
        3 轮，每轮即时探针验证）→ 成功更新 egress_node 并持久化。"""
        # TOCTOU 收口：同账号 rebalance 串行（exclude 快照→发布窗口最长 2-3 分钟，
        # 并发触发会互不可见）；跨账号并发无妨——互斥由 exclude_node 保证
        async with self._account_lock(account.id):
            return await self._rebalance_locked(account, reason, ban)

    async def _rebalance_locked(self, account, reason: str, ban: bool) -> bool:
        from . import logs
        from .store import store
        now = time.monotonic()
        if now - self._rebalance_at.get(account.id, 0) < settings.TP_REBALANCE_DEBOUNCE:
            logs.info("egress_pool", f"{account.name} rebalance 去抖窗内，跳过")
            return False
        # 去抖位延后到首次 acquire 成功才置位（review 2026-11-15：先置位会让
        # acquire 失败的 no-op rebalance 吃掉后续 120s 内更高优的 ban 触发）

        old_node = account.egress_node
        jwt = account.jwt_token or ""
        exclude = _other_nodes(account)
        probe_url = egress.tetherpool_gateway_url(account)

        for attempt in range(1, 4):
            if attempt == 1 and ban and old_node:
                await tp_ban(old_node, reason, {
                    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "account": account.name,
                }, account.name)
            d = await tp_acquire(account, force_rebalance=True, extra_exclude=exclude)
            if not d:
                logs.warn("egress_pool", f"{account.name} acquire 失败/池枯——保持旧绑定")
                return False
            self._rebalance_at[account.id] = now  # 真正开始换了才计数去抖
            node = d.get("node_name")
            exit_ip = d.get("exit_ip")
            logs.info("egress_pool",
                      f"{account.name} rebalance#{attempt} → {node} exit={exit_ip}")
            # 即时探针：经新绑定（网关 URL）真实打一针 Plan 通道
            st, raw = await self._probe_via(account, probe_url, jwt)
            if st == 0:
                # 探针连接失败（网关/节点不可达）：不 ban（连接不通≠上游封禁），
                # force_rebalance 换下一个候选
                logs.warn("egress_pool",
                          f"{account.name} 探针连接失败（{raw[:100]}）——换下一个候选")
                continue
            kind = _edge_kind(raw)
            if kind == "JSON-APP":
                account.egress_node = node
                store.update_account(account)
                # 换绑成功即清探针失败计数：旧节点累积的 streak 不能背到
                # 新节点头上，否则新节点首轮抖动就够到 2/2 误换绑（review 2026-11-15 R1）
                self.clear_probe_streak(account.id)
                # 换节点 = 出口 IP 变化：验证码 token 绑定签发出口 IP（tag 恒定
                # 但底层 IP 变了），旧 token 继续用必然 3012——清池重签
                from .captcha import captcha_manager
                captcha_manager.invalidate(probe_url)
                logs.info("egress_pool",
                          f"✅ {account.name} 出口已换 {old_node or '?'} → {node}"
                          f"（exit={exit_ip}），探针 JSON-APP 放行")
                return True
            logs.warn("egress_pool",
                      f"{account.name} 新节点 {node} 探针 {st}/{kind}——ban 后换下一个")
            await tp_ban(node, reason, {"http_status": st, "body_kind": kind,
                                        "probe": "post_rebalance"}, account.name)
            old_node = node
        # 3 轮全败：TP 服务端绑定已随最后一次 force_rebalance 移到最后候选
        # （已被 ban 或探不通）——再 acquire 一轮并探针验收，拿到可用绑定即
        # 提交（旧节点无法显式回绑：TP 不可指定节点；有 exclude + 探针兜底，
        # 至少保证 hub 记录 = TP 实际绑定 = 探针可用，互斥集合不再被污染）
        d = await tp_acquire(account, force_rebalance=True,
                             extra_exclude=_other_nodes(account))
        if d and d.get("node_name"):
            node = d.get("node_name")
            st, raw = await self._probe_via(account, probe_url, jwt)
            if st == 0 or _edge_kind(raw) != "JSON-APP":
                logs.warn("egress_pool",
                          f"{account.name} 3 轮均败，回绑候选 {node} 探针仍不过"
                          f"（TP 绑定停留在此，人工介入）")
                account.egress_node = node  # hub 记录对齐 TP 实际绑定
                store.update_account(account)
                return False
            logs.warn("egress_pool",
                      f"{account.name} 3 轮均败，第 4 轮 {node} 探针放行——恢复服务")
            account.egress_node = node
            store.update_account(account)
            return True
        logs.warn("egress_pool",
                  f"{account.name} 3 轮均败且回绑 acquire 失败"
                  f"（TP 绑定停在最后候选 {account.egress_node}，人工介入）")
        return False

    async def verify_then_rebalance(self, account, reason: str) -> None:
        """连接失败触发器（gateway 调用）：先探针验证绑定节点，探针通 = 链路
        瞬态（只留痕，不换节点防漂移）；探针不通 = 节点死 → rebalance 换绑。
        rebalance 自带 120s 去抖；同账号探针走 _account_lock 串行——N 个在飞
        请求同时失败只扇出 1 次探针（review 2026-11-15：无去重 = N 并发探针）。"""
        from . import logs

        async with self._account_lock(account.id):
            if not account.egress_node:
                await self._rebalance_locked(account, reason=reason, ban=False)
                return
            if await self.health_once(account):
                logs.info("egress_pool",
                          f"{account.name} 连接失败后探针通——瞬态抖动，保持绑定 {account.egress_node}")
                return
            logs.warn("egress_pool",
                      f"{account.name} 连接失败且探针不通（{account.egress_node}）→ rebalance")
            await self._rebalance_locked(account, reason=reason, ban=False)

    async def _probe_via(self, account, proxy_url: str, jwt: str):
        """经账号网关代理的真实 Plan 通道探针（零 strike 面：3012 归因口径）。

        连接级失败（网关/节点不可达）返回 (0, PROBE-FAIL)——不能与边缘封禁
        混淆（前者换节点，后者 TP 隔离）。"""
        import httpx

        headers = {"Authorization": "Bearer " + jwt, **_PROBE_HEADERS}
        url = settings.UPSTREAM.get("zai")
        try:
            async with httpx.AsyncClient(timeout=40, proxy=proxy_url,
                                         trust_env=False) as client:
                res = await client.post(url, content=_PROBE_BODY, headers=headers)
            return res.status_code, res.text
        except Exception as exc:  # noqa: BLE001 - 探针失败 ≠ 封禁，调用方区分处理
            return 0, f"{type(exc).__name__}: {exc}"[:140]

    # ── 健康循环 ───────────────────────────────────────────────────────────
    async def health_once(self, account) -> bool:
        """轻探针：Plan 通道经该账号网关 URL（与 rebalance 同一零 strike 口径，
        3012 JSON = 节点通、边缘放行）。连接级失败/边缘 HTML = 不健康。"""
        st, raw = await self._probe_via(
            account, egress.account_proxy(account), account.jwt_token or "")
        return st != 0 and _edge_kind(raw) == "JSON-APP"

    async def _check_account(self, account) -> None:
        """单账号健康检查（每轮并发派发）：绑定引导 → 探针 → rebalance。

        异常全部内吞（任务无人 join，逃逸即静默丢失）；强引用由
        `_inflight` 集合在任务存续期间持有，防 GC 丢任务。"""
        from . import logs
        try:
            if not account.egress_node:
                # 绑定引导（幂等，TP 粘性）：egress_node 是 ban 目标 +
                # 互斥 exclude_node 集合的来源，缺了风控约束无从落地
                if not await self.ensure_binding(account):
                    logs.warn("egress_pool",
                              f"[health] {account.name} 绑定引导失败（下轮重试）")
                    return
            if not await self.health_once(account):
                # 探针失败不直接换绑（2026-11-15 生产数据：链路秒级抖动曾连坐
                # 4 账号齐 rebalance = 无谓出口 IP churn）。连续 N 轮失败才判定
                # 节点真死；单轮失败留痕待下轮复核，探针转好即清零。
                streak = self._probe_fail_streak.get(account.id, 0) + 1
                self._probe_fail_streak[account.id] = streak
                if streak < settings.TP_HEALTH_FAIL_THRESHOLD:
                    logs.warn("egress_pool",
                              f"[health] {account.name} 出口探针失败"
                              f"（{streak}/{settings.TP_HEALTH_FAIL_THRESHOLD}，"
                              f"待下轮复核）")
                    return
                logs.warn("egress_pool",
                          f"[health] {account.name} 出口探针连续 {streak} 轮失败"
                          f" → rebalance")
                self._probe_fail_streak[account.id] = 0
                await self.rebalance(account, reason="health_probe_fail",
                                     ban=False)
            else:
                self._probe_fail_streak.pop(account.id, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单账号故障不夭折整轮
            logs.warn("egress_pool",
                      f"[health] {account.name} 探针异常（跳过）: {exc!r}")
        finally:
            self._inflight.discard(asyncio.current_task())

    async def _loop(self) -> None:
        from . import logs
        from .store import store
        while True:
            try:
                await asyncio.sleep(settings.TP_HEALTH_INTERVAL)
                if not settings.PER_ACCOUNT_EGRESS or \
                        settings.EGRESS_SUPPLY != "tetherpool":
                    continue
                accounts = [a for a in store.list_accounts("zai")
                            if a.enabled and a.provider == "zai"
                            and egress.tetherpool_enabled_for(a)]
                if not accounts:
                    continue
                for account in accounts:
                    task = asyncio.create_task(self._check_account(account))
                    self._inflight.add(task)
                # 任务自持引用（_inflight + finally discard），本轮立即返回；
                # 并发探针取代串行——7 账号 × 40s 超时串行最坏 4.6 分钟才见到死节点
                await asyncio.sleep(0)
            except Exception as exc:  # noqa: BLE001 - 健康循环永不中止
                logs.warn("egress_pool", f"健康循环异常（下轮重试）: {exc!r}")
                # 退避对齐正常节拍（2026-11-15 生产数据：30s 重试与 300s 节拍
                # 脱节，异常期 10 分钟内空转 4 轮打爆日志且打乱检查间隔）
                await asyncio.sleep(settings.TP_HEALTH_INTERVAL)

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)


egress_pool = TetherPoolEgress()
