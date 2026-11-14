"""出口一致性：验证码 token 的签发出口必须等于真实请求的出口。

阿里云无痕验证把 `verifyParam` 绑定到签发它的那次浏览会话（含出口 IP）。
官方 ZCode 客户端天然满足——SDK 在同一个 renderer 里跑、请求从同一个 socket
发出；本服务把两件事拆在不同进程，必须显式对齐，否则上游判
「unusual activity」（3012）。

历史教训（2026-10-01 claim 排障）：token 直连解、claim 走 US 代理 → 恒被拒；
两者都直连才过。当时的结论被表述成「claim 必须直连」，更准确的表述是
「token 出口必须等于请求出口」——直连只是碰巧让两边一致。

本模块只回答一个问题：某个上游 URL 的请求实际从哪个出口出去。答案必须与
httpx 的判定**逐字一致**（网关的 httpx 客户端是 trust_env=True），所以这里
复刻 httpx._utils.get_environment_proxies + URLPattern 的匹配语义，而不是
凭直觉写一套「NO_PROXY 像 curl」的规则——两者差异很大：

- `NO_PROXY=zcode.z.ai`    → 命中该 host 及其子域（httpx 键 all://*zcode.z.ai）
- `NO_PROXY=.zcode.z.ai`   → 只命中子域，**不命中** zcode.z.ai 本身
- `NO_PROXY=*.zcode.z.ai`  → 通配符被 re.escape 成字面量 `*`，**什么都不命中**
- `NO_PROXY=zcode.z.ai:443`→ 仅在端口为 443 时命中
- `NO_PROXY=*`             → 全部直连（httpx 直接返回空 proxy 表）
- `192.168.0.0/16` 这类 CIDR 同样按字面量处理，不展开

代理来源用 `urllib.request.getproxies()`（httpx 用的就是它），因此 macOS 的
系统代理配置也算数：本机没设 env 代理时 httpx 仍会走系统代理。

误判的代价不对称：把 proxy 判成 direct 会让 token 与请求换出口用（3012），
所以宁可复刻这份语义，也不要「更宽松地认为某 host 直连」。
"""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit
from urllib.request import getproxies

from . import settings

DIRECT = "direct"
PROXY = "proxy"


def _proxy_map() -> dict[str, str]:
    """httpx `_get_proxy_map` 的 scheme → 代理 子集（all/http/https）。"""
    info = getproxies()
    out: dict[str, str] = {}
    for scheme in ("http", "https", "all"):
        host = (info.get(scheme) or "").strip()
        if host:
            out[scheme] = host if "://" in host else f"http://{host}"
    return out


def proxy_url(url_or_host: str | None = None) -> str | None:
    """该目标会用的代理地址；`None` 表示无代理可走。

    httpx 按 (端口, host 长度, scheme 长度) 排序取最具体的一条，所以
    scheme 专属条目优先于 `ALL_PROXY`。不传目标时按 https 取。
    """
    hosts = _proxy_map()
    scheme = "https"
    if url_or_host:
        scheme = urlsplit(url_or_host).scheme if "//" in url_or_host else ""
        if scheme not in ("http", "https"):
            scheme = ""
    for key in ((scheme,) if scheme else ()) + ("all",):
        if key in hosts:
            return hosts[key]
    # 目标 scheme 无专属代理时，httpx 没有可匹配的 mount → 直连
    return None


def _no_proxy_entries() -> list[str]:
    return [item.strip() for item in (getproxies().get("no") or "").split(",") if item.strip()]


def _host_pattern(host_pattern: str) -> re.Pattern[str] | None:
    """httpx URLPattern 的 host 匹配（None 表示不限 host）。"""
    if not host_pattern or host_pattern == "*":
        return None
    if host_pattern.startswith("*."):
        # *.example.com：只匹配子域，不匹配 example.com 本身
        return re.compile(r"^.+\." + re.escape(host_pattern[2:]) + r"$")
    if host_pattern.startswith("*"):
        # *example.com：example.com 与任意子域
        return re.compile(r"^(.+\.)?" + re.escape(host_pattern[1:]) + r"$")
    return re.compile("^" + re.escape(host_pattern) + "$")


def _is_bare_host(entry: str) -> bool:
    """httpx 对 IPv4/IPv6/localhost 不加 `*` 前缀（按精确 host 匹配）。"""
    if entry.lower() == "localhost":
        return True
    for cast in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        try:
            cast(entry.split("/")[0])
            return True
        except ValueError:
            continue
    return False


def _mount_key(entry: str) -> tuple[re.Pattern[str] | None, int | None, str]:
    """NO_PROXY 条目 → (host 正则, 端口, scheme)。

    与 httpx 相同：条目补成 `all://*{entry}`（IP/localhost 不加 `*`，IPv6 加
    方括号），再交给 URL 解析取 host/port——所以 `host:443` 会带上端口约束。
    条目自带 `://` 时按完整 URL 模式处理。
    """
    if "://" in entry:
        raw = entry
    elif _is_bare_host(entry):
        raw = f"all://[{entry}]" if ":" in entry else f"all://{entry}"
    else:
        raw = f"all://*{entry}"
    parts = urlsplit(raw)
    scheme = "" if parts.scheme == "all" else parts.scheme
    return _host_pattern(parts.hostname or ""), parts.port, scheme


def no_proxy_covers(url_or_host: str) -> bool:
    """NO_PROXY 是否让该目标绕过代理（语义与 httpx 一致）。

    端口只认 URL 里**显式写出**的：`https://zcode.z.ai` 的 port 是 None
    （httpx.URL.port 不补默认端口），所以 `NO_PROXY=zcode.z.ai:443` 不命中它。
    传裸 host 时同理没有端口可比。
    """
    if "//" in url_or_host:
        parts = urlsplit(url_or_host)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port
    else:
        scheme, host, port = "", url_or_host, None
    host = host.strip().lower()
    if not host:
        return False
    for entry in _no_proxy_entries():
        if entry == "*":
            return True
        pattern, entry_port, entry_scheme = _mount_key(entry)
        if entry_scheme and scheme and entry_scheme != scheme:
            continue
        if pattern is not None and not pattern.match(host):
            continue
        if entry_port is not None and entry_port != port:
            continue
        return True
    return False


def egress_for(url_or_host: str) -> str:
    """该目标实际使用的出口：`proxy`（走 env/系统代理）或 `direct`。

    与 httpx `trust_env=True` 的判定一致：NO_PROXY 命中 → 直连；
    否则配了代理就走代理；都没有则直连。
    """
    if not proxy_url(url_or_host):
        return DIRECT
    return DIRECT if no_proxy_covers(url_or_host) else PROXY


# ── per-account 出口隔离（2.7.36，需求 = 每账号一个独立 IP 互相隔离）─────────
def account_proxy(account) -> str | None:
    """账号专属代理 URL；未启用 per-account 模式 / 账号未绑定 → None。

    返回 None 时调用方**保持既有共享语义**（trust_env 按 MESSAGES_FORCE_DIRECT /
    BILLING_EGRESS 等既有旋钮）；返回 URL 时调用方必须以
    `proxy=url, trust_env=False` 构建客户端——显式代理与 env 代理混用会被
    NO_PROXY 匹配歧义化。

    supply=tetherpool（2.7.39 Mode B）时返回确定性构造的网关代理 URL
    （TP 绑定服务端粘性，URL 不随 rebalance 变化——换节点是 TP 侧路由变更）。
    """
    if not settings.PER_ACCOUNT_EGRESS:
        return None
    if settings.EGRESS_SUPPLY == "tetherpool":
        return tetherpool_gateway_url(account)
    return getattr(account, "egress", None)


def tetherpool_enabled_for(account) -> bool:
    """该账号是否由 TetherPool 供给出口（supply=tetherpool 且命中灰度名单，
    名单空 = 全量）。"""
    if settings.EGRESS_SUPPLY != "tetherpool" or not settings.PER_ACCOUNT_EGRESS:
        return False
    if settings.TP_GRAY_ACCOUNTS:
        return getattr(account, "name", None) in settings.TP_GRAY_ACCOUNTS
    return True


def tetherpool_gateway_url(account) -> str:
    """per-account TetherPool 网关代理 URL（确定性构造，与 TP acquire 返回的
    gateway_proxy_url 同构）。

    username 承载路由约束（TP 网关按它解析 + 粘性绑定）；TP 侧 rebalance
    只改服务端绑定，URL 恒定 → hub 侧代理对象零重建。"""
    tp_account = "zh-" + str(getattr(account, "id", "") or "")
    username = (f"{tp_account}__type_{settings.TP_TYPE}"
                f"__region_{settings.TP_REGION}__tier_{settings.TP_TIER}"
                f"__service_{settings.TP_SERVICE}")
    host = settings.TP_GATEWAY
    if "://" not in host:
        host = "http://" + host
    return f"http://{username}:tetherpool@{host.split('://', 1)[-1]}"


def egress_tag_for(account, fallback_tag: str) -> str:
    """闸门断路器 / 验证码池 / config 缓存的隔离键。

    per-account 模式 = 账号绑定的代理 URL（每账号独立断路器与 token 池）；
    否则沿用调用方的传统 2 值 tag（proxy/direct，全局语义不变）。
    """
    return account_proxy(account) or fallback_tag


# ── 连接失败聚类（2.7.39：节点不可达 → TetherPool rebalance 的触发器）────────
# 键 = 出口 tag（per-account 模式 = 网关 URL），值 = 失败时间戳（monotonic）。
_CONN_FAIL_TIMES: dict[str, list[float]] = {}
CONN_FAIL_WINDOW = 600.0
CONN_FAIL_THRESHOLD = 3


def note_conn_fail(tag: str, now: float | None = None) -> None:
    """记录一次该出口的连接失败（调度循环内同步调用，单循环原子）。"""
    ts = time.monotonic() if now is None else now
    times = [t for t in _CONN_FAIL_TIMES.get(tag, []) if ts - t < CONN_FAIL_WINDOW]
    times.append(ts)
    _CONN_FAIL_TIMES[tag] = times


def conn_fail_clustered(tag: str) -> bool:
    """该出口在窗口内连接失败是否已达聚类阈值（节点不可达疑似）。"""
    times = [t for t in _CONN_FAIL_TIMES.get(tag, [])
             if time.monotonic() - t < CONN_FAIL_WINDOW]
    return len(times) >= CONN_FAIL_THRESHOLD


def clear_conn_fails(tag: str) -> None:
    _CONN_FAIL_TIMES.pop(tag, None)
