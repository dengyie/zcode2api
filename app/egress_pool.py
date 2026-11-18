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
import re
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


# 阿里云验证码 SDK 源（solver 经出口拉取的第一个大资源）——claim 求解链的
# 隐藏依赖。2026-11-17 1117 全灭复盘：TP 节点对它路由劣化（实测 9.9~31.9s/
# 资源，直连 0.15s）而 zcode 探针照常放行 → SDK 引导/traceless 后端签名
# 超时 → len-76 无 securityToken 降级 stub（2.7.60 拒收）+ popup 渲染不出。
ALICDN_PROBE_URL = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"


def _alicdn_fetch(proxy_url: str, timeout: float,
                  full: bool = False) -> tuple[int, str]:
    """同步抓取（asyncio.to_thread 调用）：经代理取 AliyunCaptcha.js。

    full=False 读前 64KB（健康循环轻探针：TLS+路由 TTFB 与吞吐头部，每轮 ×
    全账号全量拉会白吃出口流量）；full=True 读整文件（换绑验收强探针——
    SDK 实际拉 1MB+ 多资源串行，头部快 ≠ 整文件快，2026-11-18 review P2#9：
    trial 的 6154 全文件 10.2s 却通过 64KB 采样）。"""
    try:
        req = urllib.request.Request(
            ALICDN_PROBE_URL, method="GET",
            headers={"User-Agent": "ZCode/3.17.2"})
        with _opener(proxy_url).open(req, timeout=timeout) as r:
            if full:
                # 整文件读封顶 4MB（2026-11-18 review P3）：AliyunCaptcha.js
                # 实际 ~1MB，防异常响应（代理劫持/无限流）把 read() 撑爆内存；
                # 超限即不合格——正常文件远小于此。
                total = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 4 * 1024 * 1024:
                        return 0, f"oversize {total}B > 4MB cap"
            else:
                r.read(65536)
            return r.status, "ok"
    except Exception as exc:  # noqa: BLE001 - 路由质量探针：任何异常 = 不合格
        return 0, f"{type(exc).__name__}: {exc}"[:120]


async def probe_alicdn(proxy_url: str | None, *,
                       full: bool = False,
                       fetch_impl=None) -> tuple[bool, float, str]:
    """经代理 URL 探阿里云验证码 CDN 路由质量。返回 (ok, elapsed_s, detail)。

    ok = HTTP 200 且耗时 ≤ EGRESS_ALICDN_MAX_SECONDS；proxy_url 为空（直连
    形态）恒过——直连实测 0.15s 量级，不占探针预算。路由差 ≠ 信誉烂：
    调用方拒收候选即可，绝不能拿它 tp_ban（烧池子）。

    full=True 用整文件强探针（换绑验收，决策关键、低频）；缺省 64KB 轻探针
    （健康循环，高频）。fetch_impl 为 DI 缝（测试注入假 fetch），签名
    (proxy_url, timeout, full) -> (status, detail)。"""
    if not proxy_url:
        return True, 0.0, "direct-no-probe"
    fetch = fetch_impl or _alicdn_fetch
    timeout = float(settings.EGRESS_ALICDN_PROBE_TIMEOUT)

    def _run():
        t0 = time.monotonic()
        st, detail = fetch(proxy_url, timeout, full)
        return st, time.monotonic() - t0, detail

    st, elapsed, detail = await asyncio.to_thread(_run)
    if st != 200:
        return False, elapsed, detail
    max_s = float(settings.EGRESS_ALICDN_MAX_SECONDS)
    if elapsed > max_s:
        return False, elapsed, f"slow {elapsed:.1f}s > {max_s:g}s"
    return True, elapsed, detail


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
        from . import logs
        # 归因明细（2.7.68）：此前非 200 一律静默 None，401（token 过期）表现为
        # 「池枯」、与 TP 宕机不可分——状态码透传一条，排障不再盲猜
        logs.warn("egress_pool",
                  f"TP acquire HTTP {st}: {raw.strip()[:120]}（account={account.name}）")
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def tp_ban(node_name: str, reason: str, evidence: dict,
                 account_name: str) -> None:
    from . import logs
    # TP 服务端 reason 字段有字符集校验（非 ASCII/冒号 → HTTP 422 Invalid
    # ban reason），中文诊断文案须归一为 slug 再上报（2.7.50 实弹实证）
    reason = re.sub(r"[^A-Za-z0-9._-]+", "-", str(reason)).strip("-")[:80] or "unspecified"
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


async def tp_release_account(account_id: str) -> bool:
    """释放账号的 TP 分配（账号删除路径调用，2026-11-18 review P2 根修）。

    zh-<id> 分配在账号删除后孤儿化：TP 端无 TTL，删除账号的分配永久占节点
    （2026-11-17 实查 27 个分配里 16 个陈旧——`-b782a0d5` 族即历史删除账号），
    池压下把现役账号挤向软排除兜底（ban 节点/共享节点）。删除即释放，从源头
    杜绝陈旧分配再生。"""
    from . import logs
    q = urllib.parse.urlencode({"service": settings.TP_SERVICE,
                                "account_id": f"zh-{account_id}"})
    st, raw = await asyncio.to_thread(
        _tp_http, "POST", f"{settings.TP_API}/v1/allocation/release?{q}",
        None, _tp_headers(), 15)
    if st == 200:
        # body 的 success 字段才是释放结果（2026-11-18 review P3：HTTP 200 +
        # success:false = 无分配可释，属幂等成功而非失败）。防御非 dict JSON
        # （数组/字符串，review 复核 P3-1）。
        try:
            body = json.loads(raw)
            body_ok = body.get("success") if isinstance(body, dict) else None
        except ValueError:
            body_ok = None
        if body_ok is False:
            logs.info("egress_pool",
                      f"TP 分配释放 zh-{account_id}: 无分配可释（幂等成功）")
        else:
            logs.info("egress_pool", f"TP 分配已释放: zh-{account_id}")
        return True
    logs.warn("egress_pool",
              f"TP 分配释放失败 zh-{account_id}: HTTP {st} {raw.strip()[:100]}")
    return False


async def _tp_live_snapshot() -> tuple[dict[str, str], set[str]]:
    """TP 实时对账快照：(本 service 分配 {tp_account_id: node}, 在期隔离节点集)。

    2026-11-18 review P1 根修：exclude_node / avoid_banned 均为 TP 软语义
    （池压下会兜底返回 ban 台账在期节点、其它账号现占节点——实锤 zcodereg3
    绑上在期 ban 的 542、zcodereg4/5 同占 6157），收货前必须反查地面真相。
    拉取失败返回空快照（fail-open：冲突检查跳过，保持既有行为），调用方打
    warn 一次。"""
    import datetime as _dt

    allocs: dict[str, str] = {}
    banned: set[str] = set()
    st, raw = await asyncio.to_thread(
        _tp_http, "GET",
        f"{settings.TP_API}/v1/allocation/accounts?service={settings.TP_SERVICE}",
        None, _tp_headers(), 15)
    if st == 200:
        try:
            for a in json.loads(raw):
                allocs[a.get("account_id")] = a.get("node_name") or ""
        except ValueError:
            pass
    st, raw = await asyncio.to_thread(
        _tp_http, "GET",
        f"{settings.TP_API}/v1/feedback/bans?service={settings.TP_SERVICE}&limit=300",
        None, _tp_headers(), 15)
    if st == 200:
        try:
            # now 取 aware 本地时；台账时间 naive 视为本地（TP 与 hub 同机）、
            # aware astimezone 归一——避免 aware/naive 相减抛 TypeError 使整个
            # 快照崩掉（2026-11-18 review P3）。
            now = _dt.datetime.now().astimezone()
            for b in json.loads(raw):
                until = b.get("quarantined_until")
                if not until:
                    continue
                try:
                    u = _dt.datetime.fromisoformat(str(until))
                    if u.tzinfo is None:
                        u = u.astimezone()
                    if u > now:
                        banned.add(b.get("node_name") or "")
                except (ValueError, TypeError):
                    continue
        except ValueError:
            pass
    return allocs, banned


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
        # 去抖位改在函数收尾统一置位（2.7.68：旧实现取「首次 acquire 成功」的
        # 入口时刻，4 轮 rebalance 实测 2-3 分钟 > 120s 去抖——首个 rebalance
        # 结束瞬间下一个触发可立即再跑一轮全量 churn）。任一真实 acquire 后
        # 都盖结束时刻戳：成功换绑安静 120s，失败/churn 同样安静 120s。

        old_node = account.egress_node
        orig_node = old_node  # 原始节点恒定（old_node 会被轮内被 ban 候选改写）
        jwt = account.jwt_token or ""
        # exclude 必须含本账号要离开的旧节点（2026-11-16 生产实证：exclude 只有
        # 其它账号的节点时，force_rebalance 会把绑定换回原节点 = 假换绑——
        # trial-mvxmwyrg 05:04 日志「070 → 070」探针放行后坏节点原地保留）
        exclude = list(_other_nodes(account))
        if old_node and old_node not in exclude:
            exclude.append(old_node)
        probe_url = egress.tetherpool_gateway_url(account)

        # TP 实时对账快照（2026-11-18 review P1 根修，每轮一次）：exclude/
        # avoid_banned 是软语义，池压下 TP 会返回 ban 台账在期节点、本 service
        # 其它分配现占节点、甚至原节点本身（假换绑 6154→6154 实锤）——收货前
        # 必须反查地面真相。快照拉取失败 → 跳过冲突检查（fail-open，打 warn）。
        live_allocs, live_banned = await _tp_live_snapshot()
        own_aid = "zh-" + str(account.id)
        snapshot_ok = bool(live_allocs) or bool(live_banned)
        if not snapshot_ok:
            logs.warn("egress_pool",
                      f"{account.name} TP 实时对账快照拉取失败——本轮冲突检查跳过")

        async def _ground_truth_recheck(node: str) -> str:
            """收货前地面真相复核（2026-11-18 review P2#1）：收货快照与提交
            之间隔了探针 + alicdn 整文件验收（秒~十秒级窗口），同 service 其它
            账号的 rebalance/删除释放会在窗口内改写分配台账——提交前重拉一次。
            返回冲突描述；快照拉取失败返回空串（fail-open，与收货快照同语义）。"""
            re_allocs, re_banned = await _tp_live_snapshot()
            if not re_allocs and not re_banned:
                return ""
            if node in re_banned:
                return "在期隔离节点（复核）"
            holders = [aid for aid, n in re_allocs.items()
                       if n == node and aid != own_aid]
            if holders:
                return f"其它分配占用（{holders[0]}，复核）"
            return ""

        attempted = False   # 本轮是否真实发起过 acquire（决定收尾是否盖去抖戳）
        gw_fails = 0        # 连接级探针失败计数（网关级故障熔断）
        try:
            for attempt in range(1, 4):
                d = await tp_acquire(account, force_rebalance=True, extra_exclude=exclude)
                attempted = True
                if not d:
                    logs.warn("egress_pool", f"{account.name} acquire 失败/池枯——保持旧绑定")
                    await self._align_binding_with_tp(account, own_aid)
                    return False
                node = d.get("node_name")
                if node and node not in exclude:
                    exclude.append(node)  # 轮内已否决的候选不再回收（防 3 轮打转）
                exit_ip = d.get("exit_ip")
                logs.info("egress_pool",
                          f"{account.name} rebalance#{attempt} → {node} exit={exit_ip}")
                # 收货前三重冲突检查（顺序：假换绑 → 在期隔离 → 互斥占用；任一命中
                # 即换下一个候选，不 ban——这是 TP 分配器的锅，不是节点的锅）。
                # 假换绑检查无条件执行（old_node 是本地状态，不依赖快照）；隔离/
                # 互斥两项需要 TP 实时快照，快照坏则跳过（fail-open）。
                if node and node == old_node:
                    logs.warn("egress_pool",
                              f"{account.name} 候选 {node} == 旧节点（TP 无视 "
                              f"exclude_node 粘性返回 = 假换绑）——换下一个候选")
                    continue
                if snapshot_ok and node:
                    if node in live_banned:
                        logs.warn("egress_pool",
                                  f"{account.name} 候选 {node} 在 TP 在期隔离台账"
                                  f"（avoid_banned 兜底破约）——换下一个候选")
                        continue
                    holders = [aid for aid, n in live_allocs.items()
                               if n == node and aid != own_aid]
                    if holders:
                        logs.warn("egress_pool",
                                  f"{account.name} 候选 {node} 已被本 service 其它分配"
                                  f"占用（{holders[0]}，互斥破约）——换下一个候选")
                        continue
                # 即时探针：经新绑定（网关 URL）真实打一针 Plan 通道
                st, raw = await self._probe_via(account, probe_url, jwt)
                if st == 0:
                    # 探针连接失败（网关/节点不可达）：不 ban（连接不通≠上游封禁），
                    # force_rebalance 换下一个候选
                    gw_fails += 1
                    if settings.TP_GATEWAY_FAIL_ABORT and \
                            gw_fails >= settings.TP_GATEWAY_FAIL_ABORT:
                        # 网关级故障熔断（2.7.68）：连续 N 个候选都连接级失败 =
                        # 本地网关（EasyTier→TP 网关）故障而非节点问题——继续
                        # acquire/探针只会全池 churn 并把 egress_node 改写到从未
                        # 探通的候选（11-18 晚 EasyTier 崩溃重启期实测形态）。
                        logs.warn("egress_pool",
                                  f"{account.name} 连续 {gw_fails} 个候选探针连接级失败"
                                  f"——疑似本地网关故障，熔断本轮 rebalance"
                                  f"（TP 绑定停留在此，等网关恢复后健康循环重试）")
                        await self._align_binding_with_tp(account, own_aid)
                        return False
                    logs.warn("egress_pool",
                              f"{account.name} 探针连接失败（{raw[:100]}）——换下一个候选")
                    continue
                kind = _edge_kind(raw)
                if kind == "JSON-APP":
                    # alicdn 维度验收（2026-11-18 1117 全灭复盘）：zcode 探针放行
                    # ≠ 验证码 SDK 路由可用——alicdn 不合格的候选直接换下一个，
                    # 不 ban（路由差≠信誉烂）。exclude 已含本候选（防 3 轮打转）。
                    # 整文件强探针（full=True）：SDK 实际拉 1MB+ 多资源串行，
                    # 64KB 头部采样会放行「头部快、整文件慢」的节点（P2#9）。
                    ok_cd, elapsed, detail = await probe_alicdn(probe_url, full=True)
                    if not ok_cd:
                        logs.warn("egress_pool",
                                  f"{account.name} 新节点 {node} zcode 探针放行但 alicdn "
                                  f"路由不合格（{detail}）——换下一个候选（不 ban）")
                        continue
                    # 收货复核（review P2#1）：验收窗口内台账可能已被其它账号的
                    # rebalance/删除释放改写——提交前重查地面真相，冲突即换下一个
                    # 候选（候选已在 exclude，不会回收打转）。
                    conflict2 = await _ground_truth_recheck(node)
                    if conflict2:
                        logs.warn("egress_pool",
                                  f"{account.name} 候选 {node} 收货复核冲突"
                                  f"（{conflict2}）——换下一个候选")
                        continue
                    account.egress_node = node
                    store.update_account(account)
                    # 换绑成功即清探针失败计数：旧节点累积的 streak 不能背到
                    # 新节点头上，否则新节点首轮抖动就够到 2/2 误换绑（review 2026-11-15 R1）
                    self.clear_probe_streak(account.id)
                    # 换节点 = 出口 IP 变化：验证码 token 绑定签发出口 IP（tag 恒定
                    # 但底层 IP 变了），旧 token 继续用必然 3012——清池重签
                    from .captcha import captcha_manager
                    captcha_manager.invalidate(probe_url)
                    # ban 延后到确认离开（2.7.68）：旧实现 attempt-1 就 ban 旧节点，
                    # TP 粘性回同节点时终态 = 「绑定纹丝不动 + 唯一可用节点进隔离
                    # 台账」，且第 4 轮降级接受被自家 ban 的 re_banned_hit 永久堵死
                    # （11-18 实锤「9 次换绑未生效 + 判败人工介入」的结构性放大器）。
                    # 现在只有确认换到不同节点后才隔离最初要离开的 orig_node
                    # （old_node 已被轮内死探针候选改写，不能用作 ban 目标）。
                    if ban and orig_node and node != orig_node:
                        await tp_ban(orig_node, reason, {
                            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                            "account": account.name,
                        }, account.name)
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
                                 extra_exclude=exclude)
            if d and d.get("node_name"):
                node = d.get("node_name")
                # 第 4 轮以地面真相复核先行（review P3：冲突候选不烧探针；review
                # P2#1：收货快照已旧，判定以复核快照为准），fail-open 保持既有
                # 行为（快照坏 → 探针决定去留）。
                re_allocs, re_banned = await _tp_live_snapshot()
                re_ok = bool(re_allocs) or bool(re_banned)
                re_holders = [aid for aid, n in re_allocs.items()
                              if n == node and aid != own_aid] if re_ok else []
                re_banned_hit = node in re_banned if re_ok else False
                if re_ok and (re_banned_hit or re_holders):
                    logs.warn("egress_pool",
                              f"{account.name} 3 轮均败，第 4 轮候选 {node} 复核冲突"
                              f"（{'在期隔离' if re_banned_hit else '其它分配占用 ' + re_holders[0]}）"
                              f"——不烧探针直接判败，TP 绑定停留在此，人工介入")
                    account.egress_node = node  # hub 记录对齐 TP 实际绑定
                    store.update_account(account)
                    return False
                st, raw = await self._probe_via(account, probe_url, jwt)
                ok_cd, _elapsed, cd_detail = await probe_alicdn(probe_url, full=True)
                if re_ok and node == orig_node:
                    # P2#2 单节点池退化（2026-11-18 review）：4 轮全为「TP 无视
                    # exclude 粘性返回原节点」= 池内已无替代节点，而非分配器随机
                    # 破约。2.7.68 起 ban 已延后到确认离开之后——原节点此时不在
                    # 隔离台账，降级接受路径可达（此前 claim 的 ban=True 会把它
                    # 提前烧进台账，本分支永不可达 = 换不动即死锁人工）。条件：
                    # 不在隔离台账（复核已查）、无他人占用、全新全量验收
                    # （zcode JSON-APP + alicdn 整文件）通过 → 降级接受 + 显式
                    # 归因，区分「分配器破约」与「池枯」。
                    if st != 0 and _edge_kind(raw) == "JSON-APP" and ok_cd:
                        logs.warn("egress_pool",
                                  f"⚠️ {account.name} 4 轮全为假换绑（{orig_node}）"
                                  f"= 池内无替代节点，全新全量验收通过 → 降级接受"
                                  f"原节点（exit={d.get('exit_ip')}）继续观察；"
                                  f"节点若真坏由健康循环再触发")
                        account.egress_node = node
                        store.update_account(account)
                        self.clear_probe_streak(account.id)
                        return True
                    logs.warn("egress_pool",
                              f"{account.name} 3 轮均败，第 4 轮仍回原节点 {node} "
                              f"且验收不过（zcode={_edge_kind(raw)} alicdn={cd_detail}）"
                              f"——判败，人工介入")
                    account.egress_node = node
                    store.update_account(account)
                    return False
                if st == 0 or _edge_kind(raw) != "JSON-APP" or not ok_cd:
                    logs.warn("egress_pool",
                              f"{account.name} 3 轮均败，回绑候选 {node} 仍不过"
                              f"（zcode={_edge_kind(raw)} alicdn={cd_detail}；"
                              f"TP 绑定停留在此，人工介入）")
                    account.egress_node = node  # hub 记录对齐 TP 实际绑定
                    store.update_account(account)
                    return False
                logs.warn("egress_pool",
                          f"{account.name} 3 轮均败，第 4 轮 {node} 探针放行——恢复服务")
                account.egress_node = node
                store.update_account(account)
                if ban and orig_node and node != orig_node:
                    await tp_ban(orig_node, reason, {
                        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                        "account": account.name,
                    }, account.name)
                return True
            logs.warn("egress_pool",
                      f"{account.name} 3 轮均败且回绑 acquire 失败"
                      f"（TP 绑定停在最后候选，人工介入）")
            await self._align_binding_with_tp(account, own_aid)
            return False
        finally:
            if attempted:
                # 去抖戳 = 本轮收尾时刻（2.7.68）：成功换绑安静 120s，失败/churn
                # 同样安静 120s——故障期背靠背无限连跑 rebalance 的放大器根除
                self._rebalance_at[account.id] = time.monotonic()

    async def _align_binding_with_tp(self, account, own_aid: str) -> None:
        """hub 记录对齐 TP 实际绑定（2.7.68）：force_rebalance 已在 TP 侧移动
        绑定而 hub 未记录（round≥2 acquire 失败/网关熔断等退出路径），分叉会让
        exclude 集合与 ban 目标失真。快照拉取失败保持原值（fail-open）。"""
        from . import logs
        from .store import store
        try:
            allocs, _banned = await _tp_live_snapshot()
        except Exception as exc:  # noqa: BLE001
            logs.warn("egress_pool", f"{account.name} 绑定对齐快照失败（保持原值）: {exc!r}")
            return
        node = allocs.get(own_aid)
        if node and node != account.egress_node:
            logs.warn("egress_pool",
                      f"{account.name} hub 记录对齐 TP 实际绑定："
                      f"{account.egress_node} → {node}")
            account.egress_node = node
            store.update_account(account)

    async def verify_then_rebalance(self, account, reason: str) -> None:
        """连接失败触发器（gateway 调用）：先探针验证绑定节点，探针通 = 链路
        瞬态（只留痕，不换节点防漂移）；探针不通 = 节点死 → rebalance 换绑。
        rebalance 自带 120s 去抖；同账号探针走 _account_lock 串行——N 个在飞
        请求同时失败只扇出 1 次探针（review 2026-11-15：无去重 = N 并发探针）。

        探针盲区收口（2026-11-16 trial-mvxmwyrg 事故）：半死节点能答 16-token
        小探针却让真实流式请求中途断连（RemoteProtocolError）——探针通但连接
        失败持续聚类（CONN_FAIL_WINDOW 内 ≥ 阈值）= 指纹判死 → 强制换绑，
        不再无限期「保持绑定」。"""
        from . import logs

        tag = egress.messages_egress_tag(account)
        clustered = egress.conn_fail_clustered(tag)
        if clustered:
            logs.warn("egress_pool",
                      f"{account.name} 连接失败 {egress.CONN_FAIL_THRESHOLD} 次/"
                      f"{egress.CONN_FAIL_WINDOW:.0f}s 达聚类阈值——探针仅作佐证，"
                      f"无论结果强制 rebalance")
        async with self._account_lock(account.id):
            if not account.egress_node:
                await self._rebalance_locked(account, reason=reason, ban=False)
                egress.clear_conn_fails(tag)
                return
            if not clustered and await self.health_once(account):
                logs.info("egress_pool",
                          f"{account.name} 连接失败后探针通——瞬态抖动，保持绑定 {account.egress_node}")
                return
            logs.warn("egress_pool",
                      f"{account.name} 连接失败且{'失败聚类达阈值' if clustered else '探针不通'}"
                      f"（{account.egress_node}）→ rebalance")
            await self._rebalance_locked(account, reason=reason, ban=False)
            egress.clear_conn_fails(tag)

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
        3012 JSON = 节点通、边缘放行）。连接级失败/边缘 HTML/alicdn 路由超标
        （2026-11-18 新增维度，喂 _check_account 的 streak → rebalance）=
        不健康。"""
        proxy = egress.account_proxy(account)
        st, raw = await self._probe_via(account, proxy, account.jwt_token or "")
        if st == 0 or _edge_kind(raw) != "JSON-APP":
            return False
        ok_cd, _elapsed, _detail = await probe_alicdn(proxy)
        return ok_cd

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
            if settings.TP_ORPHAN_RECONCILE:
                task = asyncio.create_task(self._reconcile_orphans_once())
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)

    async def _reconcile_orphans_once(self, initial_delay: float = 10) -> None:
        """启动时 TP 孤儿分配对账（2.7.68）：allocs 里 zh-<id> 不在本库账号集合
        → 逐个 release。11-17 实查 27 个分配 16 个陈旧（`-b782a0d5` 族即历史
        删除账号）——删除释放只堵新增不清存量，孤儿永久占节点，池压下把现役
        账号挤向软排除兜底（ban 节点/共享节点）。只跑一次，与删除路径的实时
        释放互补；与启动期 ensure_binding 并发安全（只释放不在库的 id）。"""
        from . import logs
        from .store import store
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)  # 等 store 装载与 TP 侧稳定
        if not settings.PER_ACCOUNT_EGRESS or settings.EGRESS_SUPPLY != "tetherpool":
            return
        allocs, _banned = await _tp_live_snapshot()
        if not allocs:
            return  # 快照失败（空）= fail-open，不动任何分配
        known_ids = {a.id for provider in PROVIDERS for a in store.list_accounts(provider)}
        orphans = [aid for aid in allocs
                   if aid.startswith("zh-") and aid[3:] not in known_ids]
        if not orphans:
            logs.info("egress_pool", "[reconcile] TP 分配对账通过，无孤儿")
            return
        for aid in orphans:
            await tp_release_account(aid[3:])  # 内部自带结果日志
        logs.ok("egress_pool", f"[reconcile] 孤儿分配清理完成: {len(orphans)} 个已释放")

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
