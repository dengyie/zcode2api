"""上游请求构建。

负责根据账号凭证选择端点、组装请求头、应用 body 变换。
实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

import json
import uuid

from . import body_transform, constants, settings
from .identity import build_identity_headers, build_trace_headers
from .models import Account

# 透传客户端 header 时需要剔除的字段
_DROP_HEADERS = {
    "host",
    "content-length",
    "x-api-key",
    "authorization",
    "user-agent",
    "http-referer",
    "referer",
    "origin",
    "cookie",
    "accept",
    "accept-language",
    "accept-encoding",
    "connection",
    "true-client-ip",
    "x-original-forwarded-for",
    # 身份/追踪头由本服务仿真生成，禁止客户端透传覆盖（指纹一致性）
    "x-device-mid",
    "x-request-id",
    "x-zcode-trace-id",
    "x-zcode-session-type",
    "x-query-id",
    "x-session-id",
    "x-title",
    "x-platform",
    "x-release-channel",
    "x-client-language",
    "x-client-timezone",
    "x-os-category",
    "x-os-version",
}

# 前缀剔除：本服务仿真的头族 + 客户端 SDK 特征头族。
# x-stainless-* 是 Anthropic SDK 自动附加的运行环境指纹（lang/runtime/package 版本），
# 值来自真实调用客户端而非官方 ZCode 桌面端 —— 与 ZCode/3.10.2 的 UA 组成矛盾信号，
# 且 zapi（Node fetch 直发、无 stainless 头）长期被上游正常接受，剔除后同为已验证形状。
# x-forwarded-*/forwarded/x-real-ip/via/cf-*/cdn-loop 是反代与 CDN 隧道注入的
# 基础设施头（2026-09 review）：官方客户端永不携带，透传泄露部署拓扑并构成指纹矛盾。
_DROP_HEADER_PREFIXES = ("x-zcode", "x-stainless", "x-forwarded", "forwarded",
                         "x-real-ip", "via", "cf-", "cdn-loop")


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
    verify_region: str | None = None,
    force_fallback: bool = False,
    upstream_key: str | None = None,
) -> tuple[str, dict, bytes]:
    """返回 (目标 URL, 请求头, 序列化后的请求体)。

    body 变换（cache_control / metadata.user_id）在此统一应用：变换幂等，
    网关验证码重试时用同一 body 重建请求，重复调用安全。

    force_fallback：强制走 API Key 回退通道（Plan 通道瞬态失败如 429 时，
    账号 status 保持 ACTIVE，不能靠状态推导路由，须显式指定）。

    upstream_key：显式指定上游通道键（gateway 渠道优先梯驱动）；None 时按
    账号能力自动选：JWT → `zai`（zcode-plan 代理）→ `zai_fallback`
    （API Key）。
    """
    provider = account.provider

    if provider == "zai":
        if upstream_key:
            key = upstream_key
        elif account.uses_plan_channel() and not force_fallback:
            key = "zai"
        elif account.api_key:
            key = "zai_fallback"
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        key = upstream_key or "bigmodel"
        if key != "bigmodel" or not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    # 认证与目标 URL 选择；plan 系通道（zai）用全量身份头 + 追踪头 +
    # body transform 的「官方客户端形态」。
    if key == "zai":
        if not account.jwt_token:
            raise RuntimeError("账号缺少 JWT")
        target_url = settings.UPSTREAM["zai"]
        auth = {"Authorization": f"Bearer {account.jwt_token}"}
        plan_style = True
    elif key == "zai_fallback":
        if not account.api_key:
            raise RuntimeError("账号缺少 API Key")
        target_url = settings.UPSTREAM["zai_fallback"]
        auth = {"x-api-key": account.api_key}
        plan_style = False
    elif key == "bigmodel":
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        target_url = settings.UPSTREAM["bigmodel"]
        auth = {"x-api-key": account.api_key}
        plan_style = False
    else:
        raise RuntimeError(f"未知上游通道: {key}")

    if plan_style:
        # Plan 系通道：全量身份头 + 追踪头（对齐官方客户端 pio + trace 头序）
        from .fingerprint import profile_for

        profile = profile_for(account)
        # 会话粘性：同首轮文本 → 同 session_id（官方会话内稳定，cache 依赖它）。
        # 必须在 transform_body 之前算——日期轮会被插到 messages[0]。
        session_id = body_transform.conversation_session_id(body, account.id) or str(uuid.uuid4())
        # 轮次粘性：同一次提问（含其工具循环）→ 同 query-id（官方 3.17.2
        # attribution 语义，Magpie queryOf 同构）。同样必须在日期轮注入前算。
        query_id = body_transform.conversation_query_id(body, session_id)
        user_id = body_transform.jwt_user_id(account.jwt_token)
        model = body.get("model") if isinstance(body.get("model"), str) else None
        body = body_transform.transform_body(
            body, user_id, model,
            device_mid=profile.device_mid,
            session_id=session_id,
        )
        # Start Plan 请求头部对齐 Magpie packages/zcode/index.mjs startHeaders：
        # 不带 anthropic-beta（工具搜索 beta 仅 coding-plan 用）、不带 x-api-key
        # （仅 Authorization: Bearer JWT）。旧「vlelyanin 实证」下发的这两项
        # 已于 3.17.2 对齐时移除。
        headers = {
            "content-type": "application/json",
            **auth,
            "anthropic-version": constants.ANTHROPIC_VERSION,
            **build_identity_headers(account),
            **build_trace_headers(session_id, query_id),
        }
    else:
        # API Key 通道（回退 / bigmodel）：保持原有最小头集
        headers = {
            "content-type": "application/json",
            **auth,
            "anthropic-version": constants.ANTHROPIC_VERSION,
            "User-Agent": settings.USER_AGENT,
            "X-ZCode-App-Version": constants.X_ZCODE_APP_VERSION,
            "X-ZCode-Agent": constants.X_ZCODE_AGENT,
            "HTTP-Referer": constants.HTTP_REFERER,
        }
    if verify_param:
        headers[constants.CAPTCHA_HEADER] = verify_param
    if verify_region:
        headers[constants.CAPTCHA_REGION_HEADER] = verify_region

    # ToolSearch 别名全通道生效（api.z.ai 同症，Magpie searchless 同款）；
    # Plan 通道 transform_body 已做过，此处幂等
    body_transform.apply_tool_search_alias(body)

    for hdr, value in (incoming_headers or {}).items():
        lower = hdr.lower()
        if lower in _DROP_HEADERS or lower.startswith(_DROP_HEADER_PREFIXES):
            continue
        headers[hdr] = value

    return target_url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8")
