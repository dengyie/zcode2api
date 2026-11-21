"""鉴权依赖：后台管理密钥 + 可选的网关 API Key。"""

from __future__ import annotations

import hmac
import time

from fastapi import Header, HTTPException, Request, status

from . import settings
from .store import store

ADMIN_FAIL_LIMIT = 8
ADMIN_LOCK_SECONDS = 300

# ip -> {count, locked_until}。上限与清扫（review 2026-11-21 P1）：键是「客户端
# 身份」而非真实对端地址时（trust-proxy 下取自可伪造头），无上限 dict = 攻击者
# 每请求换一个伪造头就能既绕过锁定计数又撑爆内存——容量封顶 + 逐出最旧条目。
_failures: dict[str, dict] = {}
_FAILURES_MAX = 1024


def reset_failures() -> None:
    """测试夹具：清空登录失败计数。"""
    _failures.clear()


def _client_ip(request: Request) -> str:
    """客户端身份（锁定键）。

    信任边界（2026-11-21 review P1）：`cf-connecting-ip` / `x-real-ip` 是客户端
    可任意伪造的请求头——只有部署明确声明处于可信反向代理之后（CF Tunnel /
    nginx，由代理覆写真实值）才允许采信。缺省关（ZCODE_TRUST_PROXY_HEADERS
    未设/为 false）时用 socket 对端地址：直连部署（settings.HOST=0.0.0.0）上
    伪造头无法再绕过失败锁定。tebi canonical 部署（127.0.0.1 + CF Tunnel）须
    显式设 true 才能保持「按真实公网客户端 IP 锁定」语义。

    连坐豁免（2026-11-22 迭代二 P1 根修；同轮测试勘误收紧）：TRUST_PROXY_HEADERS=false
    且进程只监听回环地址（ZCODE_HOST=127.0.0.1）且 socket 对端为 127.0.0.1 时，
    进程不可能被外部直连——请求只能来自本机隧道进程（cloudflared，必然覆写
    cf-connecting-ip 为真实公网客户端 IP），此时按代理头锁真实来源是安全的。
    监听 0.0.0.0 的直连部署不豁免：socket=127.0.0.1 也可能是本机 curl 直连
    （伪造 cf-connecting-ip 即绕过锁定，回退到 2026-11-21 P1 修复前的形态）。
    tebi canonical 部署须显式设 ZCODE_HOST=127.0.0.1（supervisor 命令本就
    --host 127.0.0.1，与 uvicorn 直配一致）+ 不设信任头，即可同时获得
    「按真实 IP 锁定」与「无伪造面」。
    """
    if settings.TRUST_PROXY_HEADERS:
        for header in ("cf-connecting-ip", "x-real-ip"):
            val = (request.headers.get(header) or "").strip()
            if val:
                return val.split(",")[0].strip()
    socket_host = request.client.host if (request.client and request.client.host) else ""
    if socket_host == "127.0.0.1" and settings.HOST == "127.0.0.1":
        # 只监听回环 + 来自回环 = 本机隧道流量：按代理头锁真实来源
        for header in ("cf-connecting-ip", "x-real-ip"):
            val = (request.headers.get(header) or "").strip()
            if val:
                return val.split(",")[0].strip()
    return socket_host or "unknown"


def _is_locked(ip: str) -> bool:
    rec = _failures.get(ip)
    if not rec:
        return False
    until = float(rec.get("locked_until") or 0)
    if until and time.time() < until:
        return True
    if until and time.time() >= until:
        _failures.pop(ip, None)
    return False


def _record_failure(ip: str) -> None:
    if len(_failures) >= _FAILURES_MAX and ip not in _failures:
        # 逐出最旧一条（dict 保序，首键 = 最早插入且未被刷新的）：
        # 锁定语义宁可漏记一个陌生 IP 也不能无界增长
        try:
            next(iter(_failures))
            _failures.pop(next(iter(_failures)))
        except StopIteration:
            pass
    rec = _failures.setdefault(ip, {"count": 0, "locked_until": 0.0})
    rec["count"] = int(rec.get("count") or 0) + 1
    if rec["count"] >= ADMIN_FAIL_LIMIT:
        rec["locked_until"] = time.time() + ADMIN_LOCK_SECONDS


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _key_equal(token: str, key: str) -> bool:
    """常数时间比较（UTF-8 字节）：str 版 compare_digest 遇非 ASCII 会抛
    TypeError（500），等价于把「任意非 ASCII 输入」变成可观测的异常侧信道。"""
    return hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8"))


async def verify_admin_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """校验后台管理密钥。

    只接受 `Authorization: Bearer <key>` 头。查询参数（`?app_key=`）已移除
    （2.7.33）：URL 会进访问日志、浏览器历史、Referer，密钥不应出现在 URL 里。
    同一客户端连续失败达到上限后临时锁死，正确密码也要等锁过期。
    """
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录失败次数过多，请稍后再试")

    key = store.admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未配置后台密钥")

    token = _extract_bearer(authorization)
    if token is None:
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少鉴权凭证")
    if not _key_equal(token, key):
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "鉴权凭证无效")
    _failures.pop(ip, None)


async def verify_gateway_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """校验 /v1/messages 网关访问密钥（未配置则拒绝：fail-closed）。

    未配置密钥时放行等于把上游账号池暴露成公开代理——只要部署后忘了设置
    gateway key，任何人都能用你的额度。空密钥应当是「不可用」，而不是「无鉴权」
    （2.7.33）。后台首页会提示先设置网关密钥。

    失败锁定（2026-11-21 review P3，对齐 admin 面不对称缺口）：错误 key 无节流
    = /v1 端点可无限暴力试 key。复用 admin 的失败计数与锁定（同一把 _failures
    表按客户端身份分键——admin 与 gateway 对同一来源共享失败记忆，语义等价于
    「同一客户端连续乱试任何一扇门都算失败」）。
    """
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "失败次数过多，请稍后再试")

    key = store.gateway_key()
    if not key:
        # 默认 fail-closed：未配置密钥时放行等于把上游账号池暴露成公开代理。
        # 仅显式设置 ZCODE_GATEWAY_ALLOW_NO_KEY=1（本地开发）才放行。
        if settings.GATEWAY_ALLOW_NO_KEY:
            return
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "网关密钥未配置：请先在后台设置 Gateway API Key 再使用 /v1 端点",
        )
    token = _extract_bearer(authorization) or x_api_key
    if token is None:
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 API Key")
    if not _key_equal(token, key):
        _record_failure(ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API Key 无效")
    _failures.pop(ip, None)
