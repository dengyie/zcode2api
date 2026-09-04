"""上游请求构建。

负责根据账号凭证选择端点、组装请求头、应用 body 变换。
实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

import json

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
    "accept-encoding",
    "connection",
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


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
    verify_region: str | None = None,
) -> tuple[str, dict, bytes]:
    """返回 (目标 URL, 请求头, 序列化后的请求体)。

    body 变换（cache_control / metadata.user_id）在此统一应用：变换幂等，
    网关验证码重试时用同一 body 重建请求，重复调用安全。
    """
    provider = account.provider

    if provider == "zai":
        if account.mode == "jwt" and account.jwt_token:
            target_url = settings.UPSTREAM["zai"]
            auth = {"Authorization": f"Bearer {account.jwt_token}"}
        elif account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        target_url = settings.UPSTREAM["bigmodel"]
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": account.api_key}
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    if provider == "zai" and account.mode == "jwt":
        # JWT 通道：全量身份头 + 追踪头（对齐官方客户端 pio + trace 头序）
        user_id = body_transform.jwt_user_id(account.jwt_token)
        body = body_transform.transform_body(body, user_id)
        headers = {
            "content-type": "application/json",
            **auth,
            "anthropic-version": constants.ANTHROPIC_VERSION,
            **build_identity_headers(),
            **build_trace_headers(),
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

    for key, value in (incoming_headers or {}).items():
        lower = key.lower()
        if lower in _DROP_HEADERS or lower.startswith("x-zcode"):
            continue
        headers[key] = value

    return target_url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8")
