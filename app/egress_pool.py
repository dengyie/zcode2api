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
    任何时刻不允许两个账号共用一个节点）。"""
    from .store import store

    out = []
    for a in store.list_accounts("zai"):
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

    # ── 绑定/换节点编排 ────────────────────────────────────────────────────
    async def ensure_binding(self, account) -> bool:
        """懒引导：egress_node 为空的绑定账号 acquire 一次（幂等，TP 粘性）。"""
        if account.egress_node:
            return True
        d = await tp_acquire(account)
        if not d:
            return False
        account.egress_node = d.get("node_name")
        from .store import store
        store.update_account(account)
        return bool(account.egress_node)

    async def rebalance(self, account, reason: str, ban: bool = True) -> bool:
        """换该账号的 TP 绑定节点：可选 ban 旧节点 → force_rebalance（最多
        3 轮，每轮即时探针验证）→ 成功更新 egress_node 并持久化。"""
        from . import logs
        from .store import store
        now = time.monotonic()
        if now - self._rebalance_at.get(account.id, 0) < settings.TP_REBALANCE_DEBOUNCE:
            logs.info("egress_pool", f"{account.name} rebalance 去抖窗内，跳过")
            return False
        self._rebalance_at[account.id] = now

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
                logs.info("egress_pool",
                          f"✅ {account.name} 出口已换 {old_node or '?'} → {node}"
                          f"（exit={exit_ip}），探针 JSON-APP 放行")
                return True
            logs.warn("egress_pool",
                      f"{account.name} 新节点 {node} 探针 {st}/{kind}——ban 后换下一个")
            await tp_ban(node, reason, {"http_status": st, "body_kind": kind,
                                        "probe": "post_rebalance"}, account.name)
            old_node = node
        logs.warn("egress_pool", f"{account.name} 3 轮 rebalance 均未通过探针——保持旧绑定，人工复核")
        return False

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
        """轻探针：ip-api 经该账号网关 URL。True=可用。"""
        proxy = egress.account_proxy(account)
        st, raw = await asyncio.to_thread(self._http_get_via, "http://ip-api.com/json/?fields=query", proxy)
        try:
            return json.loads(raw).get("query") is not None
        except Exception:
            return False

    def _http_get_via(self, url: str, proxy: str | None):
        req = urllib.request.Request(url, headers={"User-Agent": "zcode-hub-egress-health"})
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with op.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")

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
                            if a.enabled and a.provider == "zai"]
                if not accounts:
                    continue
                for account in accounts:
                    if not await self.health_once(account):
                        logs.warn("egress_pool",
                                  f"[health] {account.name} 出口探针失败 → rebalance")
                        await self.rebalance(account, reason="health_probe_fail",
                                             ban=False)
            except Exception as exc:  # noqa: BLE001 - 健康循环永不中止
                logs.warn("egress_pool", f"健康循环异常（下轮重试）: {exc!r}")
                await asyncio.sleep(30)

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


egress_pool = TetherPoolEgress()
