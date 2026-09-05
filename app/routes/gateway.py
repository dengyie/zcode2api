"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import constants, logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..quota import fetch_quota
from ..store import store

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# 常量收口：模型表与被拒信号关键字统一在 app/constants.py
MODEL_NAME_MAP = constants.MODEL_NAME_MAP
AVAILABLE_MODELS = constants.AVAILABLE_MODELS
_EXHAUST_KEYWORDS = constants.EXHAUST_KEYWORDS


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _detect_captcha_challenge(resp: httpx.Response, text: str | None = None) -> str | None:
    """验证码挑战双检测（对齐 zapi handler.ts）。

    三种形态：
      1. 响应头 x-aliyun-captcha-verify-param 存在（官方挑战信号）
      2. HTTP 400/403 + body {"code":3007}（2026-08 观测的 body 内挑战）
      3. HTTP 403 + 文案 captcha/verify（老检测，保留兼容）
    返回挑战标记（非 None 即挑战），否则 None。
    """
    # 1) challenge 响应头
    header_val = resp.headers.get(constants.CAPTCHA_HEADER)
    if header_val and header_val.strip():
        return "header"

    if text is None:
        return None
    low = text.lower()

    # 2) body code 3007（400/403 任意状态）
    if resp.status_code in (400, 403) and any(m in text for m in constants.CAPTCHA_BODY_MARKERS):
        return "in-body-3007"

    # 3) 403 + 挑战文案
    if resp.status_code == 403 and _is_captcha_error(low):
        return "text"

    return None


def _is_exhausted(status_code: int, text: str) -> bool:
    if status_code in constants.EXHAUST_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _is_risk_control(status_code: int, text: str) -> bool:
    """风控信号判定（3012「unusual activity」/ messages 端点 405）。

    与验证码挑战互斥：调用点已先排除 challenge 形态。命中即账号级风控，
    需指数退避冷却，而非直接回传客户端错误（会导致下次立刻重打、加剧风控）。
    """
    if status_code in constants.RISK_CONTROL_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(m.lower() in low for m in constants.RISK_CONTROL_MARKERS)


def _parse_retry_after(value: str | None) -> int | None:
    """解析 Retry-After（仅秒数形态；HTTP-date 形态少见，放弃即用默认重试等待）。

    非正数不采信；超长值封顶采信 —— 尊重上游意图的同时防止把客户端吊死。
    """
    if not value:
        return None
    try:
        secs = int(float(value.strip()))
    except (ValueError, AttributeError):
        return None
    return min(secs, settings.RETRY_429_WAIT_MAX) if secs > 0 else None


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = store.select(provider, skip_ids=tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, incoming_headers, port, needs_captcha)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()


async def _try_account(req_id, account, body, incoming_headers, port, needs_captcha):
    """尝试用单个账号转发，含验证码续期与可配置重试。

    错误处理策略（参数见 settings，均可用环境变量调整）：
      - 验证码挑战：清池换码重建请求，最多 MAX_CAPTCHA_RETRIES 次
      - 429 频控：**不冷却账号**，按上游 Retry-After（封顶 RETRY_429_WAIT_MAX）
        或 RETRY_429_WAIT 等待后原地重试，最多 RETRY_429_TIMES 次；
        耗尽后换下一个账号，账号保持可用
      - 5xx 等一般错误：重试最多 RETRY_5XX_TIMES 次；耗尽后账号冷却
        COOLING_SECONDS 并换下一个账号
      - 风控（3012/405「unusual activity」真封禁）：直接禁用账号（UI 展示），
        人工确认恢复后手动启用，不做自动退避
    """
    captcha_retries = 0
    retries_429 = 0
    retries_5xx = 0
    while True:
        verify_param = verify_region = None
        if needs_captcha:
            try:
                verify_param, verify_region = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"人机校验失败: {err}")
                return JSONResponse(
                    {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                    status_code=500,
                )

        try:
            url, headers, payload = build_request(account, body, verify_param, incoming_headers, verify_region)
        except RuntimeError as err:
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            return _NEXT_ACCOUNT

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as err:
            await client.aclose()
            _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            return _NEXT_ACCOUNT

        status_code = resp.status_code

        if status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()

            # 验证码挑战：三形态任一命中即清池重试（不改账号状态）
            challenge = _detect_captcha_challenge(resp, text) if needs_captcha else None
            if challenge:
                captcha_manager.invalidate()
                captcha_retries += 1
                if captcha_retries >= MAX_CAPTCHA_RETRIES:
                    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
                    return _NEXT_ACCOUNT
                logs.warn(req_id, f"账号 {account.name} 验证码挑战（{challenge}），刷新重试")
                continue  # 同账号重建请求重试

            # 风控（3012「unusual activity」/ 405）：真封禁 → 禁用账号，人工恢复。
            # 必须先于 exhausted/其它错误判定，且不再重试（避免对封禁账号持续施压）。
            if _is_risk_control(status_code, text):
                account.ban_for_risk()
                account.last_error = (
                    f"风控封禁 (3012/unusual activity) HTTP {status_code}，"
                    f"确认恢复后请在后台手动启用（第 {account.risk_strikes} 次）"
                )
                store.update_account(account)
                logs.warn(
                    req_id,
                    f"账号 {account.name} 命中风控 HTTP {status_code}，已禁用"
                    f"（累计第 {account.risk_strikes} 次），切换下一个",
                )
                return _NEXT_ACCOUNT

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                asyncio.create_task(_safe_refresh(account))
                return _NEXT_ACCOUNT

            if status_code == 401:
                _mark(account, Status.INVALID, "鉴权失败 HTTP 401")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 403:
                # 403 已排除挑战形态（上方 challenge 分支），此处为真实鉴权拒绝
                _mark(account, Status.INVALID, "鉴权失败 HTTP 403")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 429:
                # 频控不是账号故障：不冷却，原地等一等再试，耗尽后换号且账号保持可用
                if retries_429 < settings.RETRY_429_TIMES:
                    retries_429 += 1
                    wait = _parse_retry_after(resp.headers.get("retry-after")) or settings.RETRY_429_WAIT
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 被限流 429，{wait}s 后重试"
                        f"（{retries_429}/{settings.RETRY_429_TIMES}）",
                    )
                    await asyncio.sleep(wait)
                    continue
                logs.warn(
                    req_id,
                    f"账号 {account.name} 429 重试 {settings.RETRY_429_TIMES} 次耗尽，"
                    f"切换下一个（账号保持可用）",
                )
                return _NEXT_ACCOUNT

            if status_code >= 500:
                # 一般性上游错误：重试，耗尽才冷却账号并换号
                if retries_5xx < settings.RETRY_5XX_TIMES:
                    retries_5xx += 1
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 上游 HTTP {status_code}，"
                        f"{settings.RETRY_5XX_WAIT}s 后重试（{retries_5xx}/{settings.RETRY_5XX_TIMES}）",
                    )
                    await asyncio.sleep(settings.RETRY_5XX_WAIT)
                    continue
                cool = settings.COOLING_SECONDS
                account.status = Status.COOLING
                account.cooling_until = time.time() + cool
                account.last_error = f"上游 HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却"
                store.update_account(account)
                logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，冷却 {cool}s，切换下一个")
                return _NEXT_ACCOUNT

            # 其它 4xx：直接回传客户端
            account.fail_count += 1
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            return JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            )

        # 成功：记录用量并流式透传
        account.use_count += 1
        account.last_used_at = time.time()
        account.risk_strikes = 0  # 成功即清零封禁计数
        account.last_error = None
        account.cooling_until = None
        if account.status in (Status.COOLING, Status.EXHAUSTED):
            account.status = Status.ACTIVE
        store.update_account(account)
        asyncio.create_task(_safe_refresh(account))

        content_type = resp.headers.get("content-type", "application/json")

        async def _body_iter():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                logs.req_ok(req_id)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

        out_headers = {"Cache-Control": "no-cache"}
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            # 去抖：每条消息都刷 billing 是流量放大器（会加剧风控），与 monitor 共享
            # last_checked_at，最小间隔内的刷新直接跳过
            last = account.last_checked_at
            if last and time.time() - last < settings.BILLING_REFRESH_MIN_INTERVAL:
                return
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
