"""核心网关：/v1/messages（Anthropic 风格）与 /v1/chat/completions（OpenAI 风格）。

共用多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期；OpenAI 端点由
openai_compat 做双向格式转换，调度与错误处理策略完全一致。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from .. import body_transform, business_error, constants, egress, logs, reqlog, settings
from ..affinity import affinity_manager, extract_context_key
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..gate import gate
from ..models import Account, Status
from ..openai_compat import StreamConverter, anthropic_to_openai, openai_to_anthropic
from ..quota import fetch_quota
from ..responses_compat import ResponsesStreamConverter, chat_to_response, responses_to_chat
from ..store import store
from ..token_estimate import estimate_input_tokens_after_transform

_sleep = asyncio.sleep  # 模块级引用：测试可 patch 此名而免污染全局 asyncio

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# 常量收口：模型表与被拒信号关键字统一在 app/constants.py
MODEL_NAME_MAP = constants.MODEL_NAME_MAP
AVAILABLE_MODELS = constants.AVAILABLE_MODELS
MODEL_CATALOG = constants.MODEL_CATALOG
_EXHAUST_KEYWORDS = constants.EXHAUST_KEYWORDS


def _messages_egress() -> str:
    """messages 通道有效出口 tag（同时驱动 captcha 签发出口，见 app/egress.py）。

    ZCODE_MESSAGES_FORCE_DIRECT=true 强制直连：2026-11-04 线上实证 env 代理
    出口（GCP IP）被上游持续标记——messages 走代理 17h+ 零成功（3012
    unusual activity），而同账号 billing/claim/telemetry 直连全部正常。
    统一全链路出口，消除「CN billing + US messages」双 IP 异常信号；
    置 false 恢复历史行为（走 env 代理）。
    """
    if settings.MESSAGES_FORCE_DIRECT:
        return egress.DIRECT
    return egress.egress_for(settings.UPSTREAM["zai"])


def _detect_provider(body: dict, headers) -> str:
    """入站 provider 识别（Magpie 模式命名空间：`provider/Model` 前缀）。

    优先级：模型名前缀 > `x-provider` 请求头 > 默认 zai（裸名向后兼容）。
    """
    model = body.get("model") or ""
    prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
    if prefix in ("bigmodel", "zai"):
        return prefix
    xp = (headers.get("x-provider") or "").strip().lower()
    if xp in ("bigmodel", "zai"):
        return xp
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        # 先按大小写别名改写，再兜底「已退役型号」→ 当前主力型号。上游对退役名
        # 一律 3006（model not allowed），原样透传只会让客户端拿必然失败的结果。
        mapped = MODEL_NAME_MAP.get(model.lower(), model)
        mapped = constants.RETIRED_MODEL_ALIASES.get(mapped, mapped)
        if mapped != model:
            logs.warn("gateway", f"模型 {model} → {mapped}（别名/退役型号改写）")
            body["model"] = mapped

    # 上游对 max_tokens 有硬校验（400 code 1210），钳制到合法区间并记录钳制动作
    raw = body.get("max_tokens")
    if raw is not None and not isinstance(raw, bool):
        try:
            mt = int(float(raw))
        except (TypeError, ValueError):
            mt = None
        if mt is not None:
            clamped = max(1, min(mt, constants.MAX_TOKENS_LIMIT))
            if clamped != mt:
                logs.warn("gateway", f"max_tokens {mt} 超出上游范围 [1,{constants.MAX_TOKENS_LIMIT}]，钳制为 {clamped}")
            body["max_tokens"] = clamped

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
    # 429 是频控信号，优先于一切 body 关键词：429 body 带额度文案时
    # （api.z.ai 实测形态）必须走频控重试，不得判成额度耗尽踢号。
    if status_code == 429:
        return False
    if status_code in constants.EXHAUST_HTTP_STATUSES:
        return True
    # 业务码优先：Plan 通道 1005/1113 实测形态 {"code":1005,"msg":"exceed quota limit"}
    data = _safe_json(text)
    if isinstance(data, dict) and data.get("code") in constants.EXHAUST_BUSINESS_CODES:
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


def _is_concurrency_hit(text: str) -> bool:
    """429 body 是否为「并发已满」族业务码 3008/3009/3010（区别于真频控）。

    业务码必须解析后精确比较：logid 是 20 位随机数字，裸子串 "3009" 会撞车
    （constants.CONCURRENCY_BUSINESS_CODES 注记的同款教训）；非 JSON 形态退回
    文案匹配兜底。3008/3010（账号并发/轮次并发）与 3009 同族同处置——
    「这一轮没排到」的瞬态争用，不是账号故障（magpie 插件同源实证）。
    """
    code = business_error.business_code(_safe_json(text))
    return code in constants.CONCURRENCY_BUSINESS_CODES or "concurrency limit" in text.lower()


def _is_rate_limit_hit(text: str) -> bool:
    """429/业务码是否为 1302/1303「真限流」（Z.ai 频控，非并发争用）。

    与并发相反：继续原地重试只会加重，应账号休息并下沉下一渠道。同样只做
    业务码精确比较，不做裸子串（logid 撞车教训）。
    """
    return business_error.business_code(_safe_json(text)) in constants.RATE_LIMIT_429_CODES


def _is_edge_block(status_code: int, text: str) -> bool:
    """边缘防火墙按 IP 封禁的 405（区别于账号级 3012 风控）。

    magpie 实证：3012 的 405 文案是「request has been blocked due to unusual
    activity」，属于账号风控；边缘封禁页指向「this network's address was
    blocked」，不带 code 3012。前者账号退避即可，后者说明该出口 IP 已被整体
    拒绝——继续换账号只会把全池一起送进去，必须全进程暂停（app/gate.py）。

    页面判定对齐 magpie 的宽口径（EDGE_PAGE = errors.aliyun.com | <html）：
    WAF 页改版不换本质——405 承载的任何 HTML 页都是边缘页；裸 405 / JSON
    3012 才归账号风控（下方 _is_risk_control 分支）。
    """
    if status_code not in constants.RISK_CONTROL_HTTP_STATUSES:
        return False
    if business_error.business_code(_safe_json(text)) == constants.RISK_BUSINESS_CODE:
        return False
    low = (text or "").lower()
    if any(m.lower() in low for m in constants.RISK_CONTROL_MARKERS):
        return False
    if any(m in low for m in constants.EDGE_BLOCK_MARKERS):
        return True
    # 宽口径：405 + HTML 页（Aliyun 边缘错误页形态）即按 IP 封禁处置
    return "<html" in low or "errors.aliyun.com" in low


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
    """列出可用模型（Anthropic /v1/models 风格，Magpie 命名空间前缀）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in MODEL_CATALOG
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (ClientDisconnect, json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, "messages", str(body.get("model") or "-"),
                 bool(body.get("stream")), _last_user_text(body))
    # 客户端指纹（2026-11-04 排障加装）：周期短探测打线上时无法定位来源——
    # CF 隧道绕过 nginx 无访问日志、hub 也不记录调用方。真实客户端 IP 以
    # CF-Connecting-IP 为准（经 cloudflared），直连本机时回退 X-Forwarded-For /
    # 本地环回。UA 截断防日志爆炸。
    _client_ip = (incoming_headers.get("cf-connecting-ip")
                  or incoming_headers.get("x-forwarded-for") or "local")
    logs.info(req_id, f"client={_client_ip} ua={incoming_headers.get('user-agent', '?')[:72]}")

    try:
        result = await _dispatch(req_id, body, incoming_headers, provider)
    except asyncio.CancelledError:
        # 客户端在调度期间断开（429 重试/验证码等待可达数分钟）——CancelError
        # 是 BaseException，不兜底会让监控条目永久滞留「进行中」
        reqlog.finish_error(req_id, "客户端断开", status=499)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if isinstance(result, _Upstream):
        # dispatch 返回与流式生成器启动之间的取消窗口：兜底关闭释放并发槽位
        try:
            return result.to_streaming(req_id)
        except asyncio.CancelledError:
            await result.close()
            raise
    return result


async def _run_compat_endpoint(request: Request, kind: str, translate,
                               stream_hook, finalize):
    """OpenAI 风格端点公共骨架（/v1/chat/completions 与 /v1/responses 共用）。

    流程：解析 JSON → translate(payload) 得 anthropic body → 归一 → 调度
    （多账号/重试/验证码与 /v1/messages 同一条路径）→ 按客户端要求走流式或
    非流式消费。三方差异全部参数化：
      translate(payload)  -> (anthropic_body | None, err | None)
      stream_hook(up, model, req_id) -> StreamingResponse
      finalize(data, model) -> dict   # 非流式：anthropic message → 客户端格式
    """
    try:
        payload = await request.json()
    except (ClientDisconnect, json.JSONDecodeError, ValueError):
        # ClientDisconnect：客户端在读请求体阶段断开——回包无人收，仅为不刷
        # starlette 完整堆栈（err.log 曾每次断开一条 Traceback）
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}}, status_code=400)

    body, err = translate(payload)
    if err or body is None:
        return JSONResponse({"error": {"message": err or "请求体不合法", "type": "invalid_request_error"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(payload.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, kind, str(body.get("model") or "-"),
                 bool(payload.get("stream")), _last_user_text(body))

    try:
        result = await _dispatch(req_id, body, incoming_headers, provider)
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if not isinstance(result, _Upstream):
        return result

    model = str(body.get("model") or "")
    if payload.get("stream"):
        try:
            return stream_hook(result, model, req_id)
        except asyncio.CancelledError:
            await result.close()
            raise

    try:
        raw = await result.resp.aread()
        logs.req_ok(req_id)
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499, t_first=result.t_first)
        raise
    except Exception as err:  # noqa: BLE001
        logs.req_err(req_id, f"读取上游响应失败: {err}")
        reqlog.finish_error(req_id, f"读取上游响应失败: {err}", status=502)
        return JSONResponse({"error": {"message": f"读取上游响应失败: {err}", "type": "upstream_error"}}, status_code=502)
    finally:
        await result.close()
    data = _safe_json(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict) or data.get("type") != "message":
        reqlog.finish_error(req_id, "上游响应格式异常", status=502, t_first=result.t_first)
        return JSONResponse({"error": {"message": "上游响应格式异常", "type": "upstream_error"}}, status_code=502)
    usage = data.get("usage") or {}
    reqlog.finish_ok(req_id, t_first=result.t_first, status=result.resp.status_code,
                     input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
    return JSONResponse(finalize(data, model))


@router.post("/v1/chat/completions", dependencies=[Depends(verify_gateway_key)])
async def chat_completions(request: Request):
    """OpenAI Chat Completions 兼容端点（openai_compat 双向转换）。"""
    return await _run_compat_endpoint(
        request, "chat",
        translate=openai_to_anthropic,
        stream_hook=_openai_stream_response,
        finalize=anthropic_to_openai,
    )


def _translate_responses(payload: dict) -> tuple[dict | None, str | None]:
    """Responses → Chat Completions → Anthropic 两跳翻译。"""
    chat, err = responses_to_chat(payload)
    if err or chat is None:
        return None, err
    return openai_to_anthropic(chat)


@router.post("/v1/responses", dependencies=[Depends(verify_gateway_key)])
async def responses(request: Request):
    """OpenAI Responses API 兼容端点（入站 Responses → chat → anthropic，出站反向）。

    调度、重试、换号、验证码与 /v1/chat/completions 完全一致（公共骨架
    `_run_compat_endpoint`），只在两头做形状翻译。
    """
    return await _run_compat_endpoint(
        request, "responses",
        translate=_translate_responses,
        stream_hook=_responses_stream_response,
        finalize=lambda data, model: chat_to_response(anthropic_to_openai(data, model), model),
    )


@router.post("/v1/messages/count_tokens", dependencies=[Depends(verify_gateway_key)])
async def count_tokens(request: Request):
    """Anthropic count_tokens 端点：本地确定性估算。

    上游（Plan 通道与 api.z.ai）**没有**这个端点（2026-11-02 实测 404），但
    Anthropic SDK / Claude Code 会先调它做上下文预算，缺了会直接中断会话。
    这里只做估算：不占账号额度、不打上游、不需要可用账号（池空也能回）。

    估算对象是 **transform 之后**的形状（含官方 system 三块 + 日期轮，
    线上标定固定开销 ≈400 tok）——2026-11-03 实测：只算客户端原始 body 时
    6 tok 的请求实际计费 341，预算严重失真。
    """
    try:
        body = await request.json()
    except (ClientDisconnect, json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return JSONResponse({"error": {"message": "必须提供 model 参数", "type": "invalid_request_error"}}, status_code=400)
    return {"input_tokens": estimate_input_tokens_after_transform(_normalize_body(body), model)}


def _openai_stream_response(up: _Upstream, model: str, req_id: str) -> StreamingResponse:
    """把上游 Anthropic SSE 事件流转换为 OpenAI chunk 流。"""
    conv = StreamConverter(model)

    async def _iter():
        try:
            yield conv.start()
            async for line in up.resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                evt = _safe_json(data_str)
                if isinstance(evt, dict):
                    for out in conv.feed(evt):
                        yield out
            yield conv.done()
            logs.req_ok(req_id)
            reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code,
                             input_tokens=conv.usage.get("prompt_tokens"),
                             output_tokens=conv.usage.get("completion_tokens"))
        except asyncio.CancelledError:
            reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first)
            raise
        except Exception as err:  # noqa: BLE001
            logs.req_err(req_id, f"流传输中断: {err}")
            reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first)
        finally:
            await up.close()

    return StreamingResponse(_iter(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _responses_stream_response(up: _Upstream, model: str, req_id: str) -> StreamingResponse:
    """上游 Anthropic SSE → Responses 事件族（经 chat chunk 中转，复用两套转换器）。"""
    chat_conv = StreamConverter(model)
    resp_conv = ResponsesStreamConverter(model)

    async def _iter():
        try:
            for out in resp_conv.start():
                yield out
            async for line in up.resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                evt = _safe_json(data_str)
                if not isinstance(evt, dict):
                    continue
                for chunk in chat_conv.feed_events(evt):
                    for out in resp_conv.feed_chunk(chunk):
                        yield out
            for out in resp_conv.done():
                yield out
            logs.req_ok(req_id)
            reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code,
                             input_tokens=chat_conv.usage.get("prompt_tokens"),
                             output_tokens=chat_conv.usage.get("completion_tokens"))
        except asyncio.CancelledError:
            reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first)
            raise
        except Exception as err:  # noqa: BLE001
            logs.req_err(req_id, f"流传输中断: {err}")
            reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first)
        finally:
            await up.close()

    return StreamingResponse(_iter(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _provider_chain(provider: str) -> list[str]:
    """跨 provider 降级链（Magpie 模式）。

    首选显式指定的 provider，其后按 `settings.PROVIDER_FALLBACK_ORDER` 里
    位于它之后的 provider 补齐（默认 zai → bigmodel）。裸模型名与 `zai/`
    前缀都落在 zai，zai 池没号时自动下沉 bigmodel；`bigmodel/` 前缀只在
    bigmodel 池内调度（order 里它已是最后一站）。
    """
    order = list(settings.PROVIDER_FALLBACK_ORDER)
    if provider in order:
        return order[order.index(provider):]
    return [provider]


async def _dispatch(req_id, body, incoming_headers, provider):
    """多账号负载均衡调度：_Upstream（成功）或 JSONResponse（错误）。

    调度准则：
    1. 同一上下文亲和性：多轮请求绑定同一账号，提高 Cache 命中与局部性；
    2. 新请求多账号负载均衡：分配到在飞负载最低的账号（同负载轮询平局）；
    3. 单账号并发限制：写死 2，满载或故障时无缝溢出至其他可用账号；
    4. 满载跳过不计入 MAX_ACCOUNT_ATTEMPTS；全满/无号 → 503；
    5. 跨 provider 降级：当前 provider 池无可用号时，按 PROVIDER_FALLBACK_ORDER
       下沉到下一 provider（Magpie 模式，zai 池空自动用 bigmodel）。
    """
    ctx_info = extract_context_key(body, incoming_headers, provider=provider)
    pinned_id = affinity_manager.get(ctx_info.key) if ctx_info.is_continuation else None
    if pinned_id:
        logs.info(req_id, f"会话亲和命中: key={ctx_info.key} -> account={pinned_id}")

    tried: set[str] = set()
    limit = _limit()
    attempts = 0
    providers = _provider_chain(provider)

    for pi, active_provider in enumerate(providers):
        while attempts < MAX_ACCOUNT_ATTEMPTS:
            account = store.select(
                active_provider,
                skip_ids=tried,
                inflight=_inflight if limit > 0 else None,
                pinned_id=pinned_id,
                max_inflight=limit if limit > 0 else 2,
            )
            if account is None:
                break  # 本 provider 池已无可选账号 → 下沉下一 provider
            tried.add(account.id)
            pinned_id = None

            slot_box: list[str | None] = [None]
            if limit > 0:
                if _inflight.get(account.id, 0) >= limit:
                    logs.warn(req_id, f"账号 {account.name} 并发已满（{_inflight.get(account.id, 0)}/{limit}），切换下一个")
                    continue
                # 在任何异步挂起点（await）前同步占槽，消除 Check-then-Act 竞态击穿上限
                _inflight[account.id] = _inflight.get(account.id, 0) + 1
                slot_box[0] = account.id

            attempts += 1
            # 结构上能否用到验证码（Plan 通道）；是否**真的带**由上游 captcha 配置决定：
            # 线上 skip_model_request=true ⇒ 官方客户端模型请求不带验证码头，hub 也必须不带
            # （带了就是「官方不会干的事」，3012 的结构性嫌疑）。上游若仍回 3007 要码，
            # _try_account 会补码重试。渠道梯内 zcode-plan(`zai`) 才用验证码。
            plan_channel = account.provider == "zai" and account.uses_plan_channel()
            try:
                needs_captcha = plan_channel and await captcha_manager.model_requests_need_captcha(
                    _messages_egress()
                )
                result = await _try_account(
                    req_id, account, body, incoming_headers, plan_channel, needs_captcha,
                    slot_box,
                )
            except BaseException:
                if slot_box[0] is not None:
                    _release_slot(slot_box[0])
                    slot_box[0] = None
                raise
            if result is _NEXT_ACCOUNT:
                if slot_box[0] is not None:
                    _release_slot(slot_box[0])
                    slot_box[0] = None
                continue
            if isinstance(result, _Upstream):
                if ctx_info.key:
                    affinity_manager.bind(ctx_info.key, account.id)
                held = slot_box[0]
                slot_box[0] = None
                if held is not None:
                    result.on_close = _make_slot_releaser(held)
                return result
            if slot_box[0] is not None:
                _release_slot(slot_box[0])
                slot_box[0] = None
            # 仅在上游流成功建立（_Upstream）时才记录会话亲和；4xx 等错误不建立绑定
            return result

        if pi + 1 < len(providers):
            logs.info(
                req_id,
                f"provider {active_provider} 池无可选账号，跨 provider 降级 "
                f"{providers[pi + 1]}",
            )

    # 进程级闸门拒绝（边缘封禁/忙碌断路器/分钟预算）期间的兜底：所有 plan 账号
    # 都被本地拒绝到这里，必须带真实原因与 Retry-After，而不是误导性的
    # 「无可用账号」。rate_limit 是限流语义回 429，其余回 503。
    reason = gate.refusal_reason()
    if reason:
        wait = gate.retry_after()
        status = 429 if reason == "rate_limit" else 503
        logs.req_err(req_id, f"闸门 {reason}，拒绝本请求（Retry-After {wait}s）")
        reqlog.finish_error(req_id, f"gate_{reason}", status=status)
        return JSONResponse(
            {"error": {"message": f"上游保护闸门触发（{reason}），请 {wait}s 后重试",
                       "type": f"gate_{reason}"}},
            status_code=status,
            headers={"Retry-After": str(wait)},
        )

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽 / 并发已满")
    reqlog.finish_error(req_id, "无可用账号 / 额度均已耗尽 / 并发已满", status=503)
    return JSONResponse(
        {"error": {"message": "所有账号均不可用、额度已用完或并发已满，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


def _business_error_code(data: dict) -> int | None:
    """HTTP 200 承载的业务错误码（Plan 通道实测形态 `{"code":1005,"msg":...}`）。

    实现在 `app.business_error`（gateway / quota 共用一份判定，避免两处漂移）。
    """
    return business_error.business_code(data)


def _status_for_business_code(code: int) -> int:
    """业务码 → 等价 HTTP 状态（复用下方已有的错误分支，不另起一套处置）。

    这里**不写死映射表**：风控码与额度码都取自 constants，改常量即同步生效，
    不会出现「常量加了 1113、映射忘了」这类静默漂移（单测把两边对齐钉住）。
    """
    if code == constants.RISK_BUSINESS_CODE:
        return constants.RISK_CONTROL_HTTP_STATUSES[0]
    if code in constants.EXHAUST_BUSINESS_CODES:
        return constants.EXHAUST_HTTP_STATUSES[0]
    if code in constants.RATE_LIMIT_429_CODES:
        return 429
    return 400


def _buffered_response(resp: httpx.Response, raw: bytes) -> httpx.Response:
    """把已读净的正文包成内存响应，供下游按原语义消费。

    去掉 content-encoding/content-length/transfer-encoding：正文已是解码后的
    字节，保留这些头会让下游再解一次（httpx 对内存响应仍会解 content-encoding）。
    """
    drop = ("content-encoding", "content-length", "transfer-encoding")
    headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in drop]
    return httpx.Response(resp.status_code, headers=headers, content=raw)


def _release_slot(account_id: str) -> None:
    n = _inflight.get(account_id, 0) - 1
    if n <= 0:
        _inflight.pop(account_id, None)
    else:
        _inflight[account_id] = n


def _park_slot(slot_box: list[str | None] | None) -> None:
    """等待（429/验证码/5xx）前释放并发槽，避免把账号冻住数分钟。"""
    if slot_box and slot_box[0] is not None:
        _release_slot(slot_box[0])
        slot_box[0] = None


def _reacquire_slot(account: Account, slot_box: list[str | None] | None) -> bool:
    """等待结束后重新占槽；占不到则让调用方换号。"""
    if slot_box is None:
        return True
    limit = _limit()
    if limit <= 0:
        return True
    if _inflight.get(account.id, 0) >= limit:
        return False
    _inflight[account.id] = _inflight.get(account.id, 0) + 1
    slot_box[0] = account.id
    return True


def _make_slot_releaser(account_id: str):
    def _release() -> None:
        _release_slot(account_id)
    return _release


_NEXT_ACCOUNT = object()


# fire-and-forget 后台任务强引用：事件循环对 task 只持弱引用，裸 create_task
# 会被 GC 中途静默丢弃（同 main.py 启动安装序已修过的缺陷，2026-09 review）。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# 账号并发限制：account_id → 在飞请求数。asyncio 单线程事件循环下
# check+inc 原子；释放走 _Upstream.close / 失败路径，泄漏面在测试钉住。
_inflight: dict[str, int] = {}


def _limit() -> int:
    """当前并发上限。写死 2（2026-11-04 线上实测），env/meta 一律无效。"""
    return store.account_concurrency()


class _Upstream:
    """已建立的上游成功流：由调用方消费并负责关闭。"""

    __slots__ = ("resp", "cm", "client", "t_first", "account_name", "mode",
                 "on_close", "gate_release", "_closed")

    def __init__(self, resp: httpx.Response, cm, client: httpx.AsyncClient | None,
                 t_first: float | None = None, account_name: str = "", mode: str = "",
                 on_close=None, gate_release=None) -> None:
        self.resp = resp
        self.cm = cm
        self.client = client
        self.t_first = t_first
        self.account_name = account_name
        self.mode = mode
        self.on_close = on_close
        self.gate_release = gate_release
        self._closed = False

    async def close(self) -> None:
        """幂等关闭：释放上游流、并发槽位与进程级闸门，重复调用安全。

        cm/client 可能为 None（200 业务错误的正文已读净并包成内存响应），
        只释放仍然存在的部分。
        """
        if self._closed:
            return
        self._closed = True
        if self.cm is not None:
            await self.cm.__aexit__(None, None, None)
        if self.client is not None:
            await self.client.aclose()
        for callback in (self.on_close, self.gate_release):
            if callback is None:
                continue
            try:
                callback()
            except Exception:  # noqa: BLE001 - 槽位/闸门释放失败不掩盖主流程
                pass

    def to_streaming(self, req_id: str) -> StreamingResponse:
        """原样透传（/v1/messages 直通路径）。

        ToolSearch 别名反向还原（Magpie searchNamed 同款）：请求侧把客户端的
        ToolSearch 改名成了 ToolSearchTool（body_transform），模型对别名的
        调用在这里还原回原名，客户端看到的一直是自己的工具名。对不含别名的
        响应只有一次 memchr 级扫描，热路径开销可忽略；跨分块截断的 token 由
        尾部保留窗处理。/v1/messages 的流式与非流式都走这里，一处覆盖两态。
        """
        up = self

        async def _body_iter():
            alias = body_transform.TOOL_SEARCH_ALIAS.encode()
            name = body_transform.TOOL_SEARCH.encode()
            keep = len(alias) - 1  # 尾部保留窗：token 可能被分块边界截断
            tail = b""
            try:
                async for chunk in up.resp.aiter_bytes():
                    buf = tail + chunk
                    if alias not in buf:
                        # 快路径：本窗无别名（含截断形态）——但 buf 末尾 keep-1 字节
                        # 仍可能是被截断的别名前缀，必须留到下一窗拼接
                        if len(buf) > keep:
                            yield buf[:-keep]
                            tail = buf[-keep:]
                        else:
                            tail = buf
                        continue
                    if len(buf) > keep:
                        head, tail = buf[:-keep], buf[-keep:]
                    else:
                        head, tail = b"", buf
                    if alias in head:
                        head = head.replace(alias, name)
                    if head:
                        yield head
                if tail:
                    yield tail.replace(alias, name)
                logs.req_ok(req_id)
                reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code)
            except asyncio.CancelledError:
                reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first)
                raise
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
                reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first)
            finally:
                await up.close()

        return StreamingResponse(_body_iter(), status_code=up.resp.status_code,
                                 media_type=up.resp.headers.get("content-type", "application/json"),
                                 headers={"Cache-Control": "no-cache"})


def _channel_ladder(account: Account) -> list[str]:
    """账号渠道优先梯（Magpie 模式）。

    zai：zcode_plan（jwt @ zcode-plan 代理）→ apikey（api.z.ai）；
    bigmodel：bigmodel（api key）。仅当账号确持对应凭证才入梯。
    """
    if account.provider == "zai":
        ladder: list[str] = []
        if account.uses_plan_channel():
            ladder.append("zai")
        if bool((account.api_key or "").strip()):
            ladder.append("zai_fallback")
        return ladder
    if account.provider == "bigmodel":
        return ["bigmodel"] if bool((account.api_key or "").strip()) else []
    return []


def _parse_exhaust_reset(text: str) -> float | None:
    """从额度耗尽文案中尽力解析重置时间戳（仅未来时间）；解析不到返回 None。

    1310「已达周/月使用上限」可能带下一次重置时间（ISO/日期时间形态）。
    解析失败不阻塞主流程——额度恢复最终由 billing 刷新兜底。
    """
    m = re.search(
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|[T\s]+)?"
        r"(?:(\d{1,2})[:时](\d{2})(?:[:分](\d{2}))?)?",
        text,
    )
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        ts = datetime.datetime(y, mo, d, hh, mm, ss).timestamp()
    except (ValueError, OSError):
        return None
    return ts if ts > time.time() else None


async def _try_account(req_id, account, body, incoming_headers, plan_channel,
                       needs_captcha, slot_box: list | None = None):
    """尝试用单个账号转发，含渠道优先梯、验证码续期与可配置重试。

    渠道梯（Magpie 模式，见 _channel_ladder）：
      zai：zcode_plan（jwt @ zcode-plan 代理）→ apikey（api.z.ai）
    错误处理策略（参数见 settings，均可用环境变量调整）：
      - 验证码挑战：清池换码重建请求，最多 MAX_CAPTCHA_RETRIES 次（仅 zcode_plan）
      - 429 按业务码分流：
        · 3008/3009/3010（并发族）→ 短退避重试，不算账号故障；同一请求连续
          两次被拒计一次真饱和信号，累计触发进程级 busy-hold（app/gate.py）
        · 1302/1303（真限流）→ 账号休息 RATE_LIMIT_COOLDOWN 秒并下沉下一渠道
        · 其它（频控）→ **不冷却账号**，按上游 Retry-After（封顶
          RETRY_429_WAIT_MAX）或 RETRY_429_WAIT 等待后原地重试，最多
          RETRY_429_TIMES 次；耗尽后下沉下一渠道（plan → apikey），再耗尽
          换号且账号保持可用
      - 5xx 等一般错误：重试最多 RETRY_5XX_TIMES 次；耗尽后账号冷却
        COOLING_SECONDS 并换下一个账号
      - 风控（3012/405「unusual activity」）：指数退避冷却（base 900s 起，封顶
        24h），累计 RISK_BAN_STRIKES 次升级禁用（UI 展示），人工确认恢复
      - 边缘 405（HTML 封 IP 页）：全进程暂停 plan 渠道 GATE_EDGE_HOLD_SECONDS，
        不冷却/禁用任何账号（被封的是出口 IP，不是账号）
      - 1310（周/月使用上限）：额度耗尽信号，标记 EXHAUSTED 并尽力解析重置
        时间写入 cooling_until（不是频控，不重试）
    """
    ladder = _channel_ladder(account)
    if not ladder:
        account.record_result(False, "无可用渠道凭证")
        _mark(account, Status.INVALID, "无可用渠道凭证")
        logs.warn(req_id, f"账号 {account.name} 无可用渠道凭证，切换下一个")
        return _NEXT_ACCOUNT

    channel_idx = 0
    channel = ladder[0]
    captcha_retries = 0
    retries_429 = 0
    retries_5xx = 0
    model_name = str(body.get("model") or "-")
    # 验证码必须与本次 zcode-plan 请求同出口（见 app/egress.py）；记下来供失败日志佐证
    captcha_egress = _messages_egress() if plan_channel else None
    gate_acquired = False

    def _drop_gate() -> None:
        """归还闸门槽位。等待后 continue 会重新申请——每次上游尝试都算一次预算；
        长等待期间不占用在飞名额（否则 GATE_MAX_INFLIGHT 很小会把整池冻住）。"""
        nonlocal gate_acquired
        if gate_acquired:
            gate.release()
            gate_acquired = False

    def _jump_to_channel(target: str | None = None) -> bool:
        """下沉到渠道梯下一站（target 指定目标渠道）；无可达下一站返回 False。"""
        nonlocal channel_idx, channel, captcha_retries, retries_429, retries_5xx
        if target is None:
            target_idx = channel_idx + 1
        else:
            if target not in ladder:
                return False
            target_idx = ladder.index(target)
            if target_idx <= channel_idx:
                return False  # 已在该渠道或其后，无可再降
        if target_idx >= len(ladder):
            return False
        channel_idx = target_idx
        channel = ladder[channel_idx]
        captcha_retries = 0
        retries_429 = 0
        retries_5xx = 0
        return True

    def _jump_to_fallback() -> bool:
        """风控/鉴权后跳过所有 plan 系渠道，直达 api-key 渠道（如持有）。"""
        fallback = "zai_fallback" if account.provider == "zai" else "bigmodel"
        return _jump_to_channel(target=fallback)

    try:
        while True:
            attempt_t0 = time.time()
            reqlog.mark_account(req_id, account.name, account.mode)
            verify_param = verify_region = None
            # 仅 zcode-plan（`zai`）渠道用验证码；apikey 免码
            if channel == "zai" and needs_captcha:
                # 闸门会拒（边缘封禁/忙碌/分钟预算）时不要先花一枚真浏览器解出的
                # 验证码再被拒掉：池上限只有 2 枚/出口，解一枚 10–40s，空池会把
                # 每个请求都拖进求解。
                refuse = gate.refusal_reason()
                if refuse:
                    if _jump_to_fallback():
                        logs.warn(req_id, f"账号 {account.name} 闸门 {refuse}，直接切 API Key 回退")
                        needs_captcha = False
                        continue
                    # 进程级 hold 与账号无关：换下一个 plan 账号同样会被本地拒绝
                    #（有 Key 的账号已在上面下沉），交给 dispatch 兜底 503——带真实
                    # 原因与 Retry-After，而不是在这里终止整个调度
                    return _NEXT_ACCOUNT
                _park_slot(slot_box)
                try:
                    # 验证码必须与本次 Plan 请求同出口：token 由某出口签发后，
                    # 换出口使用会被上游判 unusual activity（3012）。
                    verify_param, verify_region = await captcha_manager.get_verify_param(
                        egress_tag=_messages_egress()
                    )
                except Exception as err:  # noqa: BLE001
                    logs.req_err(req_id, f"人机校验失败: {err}")
                    reqlog.finish_error(req_id, f"人机校验失败: {err}", status=500)
                    return JSONResponse(
                        {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                        status_code=500,
                    )
                if not _reacquire_slot(account, slot_box):
                    logs.warn(req_id, f"账号 {account.name} 验证码等待后并发已满，切换下一个")
                    return _NEXT_ACCOUNT

            try:
                url, headers, payload = build_request(
                    account, body, verify_param, incoming_headers, verify_region,
                    upstream_key=channel,
                )
            except RuntimeError as err:
                # 本渠道凭证缺失/无效：先下沉下一渠道，梯底才判死账号
                account.record_result(False, f"渠道 {channel} 凭证无效: {err}")
                logs.warn(req_id, f"账号 {account.name} 渠道 {channel} 凭证无效: {err}，下沉下一渠道")
                if _jump_to_channel():
                    continue
                _mark(account, Status.INVALID, str(err))
                return _NEXT_ACCOUNT

            # 进程级闸门：只对 zcode-plan 渠道生效。被拒时优先下沉 API Key 渠道
            # （api.z.ai 与 IP 封禁的风险面不同）；无可下沉时换号，交给 dispatch
            # 兜底 503（带 gate 原因与 Retry-After）——不在这里终止整个调度，否则
            # 混合池里排在后面的有 Key 账号永远轮不到。
            gate_acquired = False
            if channel == "zai":
                ok, why = gate.acquire()
                if not ok:
                    if _jump_to_fallback():
                        logs.warn(req_id, f"账号 {account.name} 闸门 {why}，切 API Key 回退")
                        needs_captcha = False
                        continue
                    return _NEXT_ACCOUNT
                gate_acquired = True

            client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
                # ZCODE_MESSAGES_FORCE_DIRECT：直连出口须绕过 env/系统代理（与
                # _messages_egress 的 DIRECT tag 一致，否则 captcha 出口错配 3012）
                trust_env=not settings.MESSAGES_FORCE_DIRECT,
            )
            cm = client.stream("POST", url, headers=headers, content=payload)
            try:
                resp = await cm.__aenter__()
            except httpx.HTTPError as err:
                await client.aclose()
                _drop_gate()
                account.record_result(False, f"连接失败: {err}")
                # 废 JWT / 风控禁用走 Key 回退失败时不得洗成 cooling，否则冷却结束会重开 Plan
                if account.status in (Status.INVALID, Status.DISABLED):
                    store.update_account(account)
                else:
                    _mark(account, Status.COOLING, f"连接失败: {err}")
                logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
                return _NEXT_ACCOUNT

            status_code = resp.status_code
            text: str | None = None

            # 200 承载业务错误：Plan 通道对额度/风控类拒绝直接回 HTTP 200 +
            # {"code":1005|3012,"msg":...}（2026-10-30 实测，content-type application/json）。
            # 不识别有两个后果：① 当成成功洗掉 risk_strikes / 冷却 / last_error，把仍在
            # 标记期的账号拉回 ACTIVE（「冷却期零上游流量」不变量被绕开）；② 错误体当正文
            # 透传，客户端拿到「200 但没有 message」。JSON 正文一次读净：合法响应改走内存
            # 流（下游消费逻辑不变），业务错误码映射成等价 HTTP 状态走下面统一分支。
            if status_code < 400 and resp.headers.get("content-type", "").startswith("application/json"):
                raw = await resp.aread()
                await cm.__aexit__(None, None, None)
                await client.aclose()
                cm = client = None  # 已关闭，_Upstream.close 容忍 None
                decoded = raw.decode("utf-8", "ignore")
                data = _safe_json(decoded)
                code = _business_error_code(data) if isinstance(data, dict) else None
                if code is None:
                    resp = _buffered_response(resp, raw)
                else:
                    text = decoded
                    status_code = _status_for_business_code(code)
                    logs.warn(
                        req_id,
                        f"账号 {account.name} HTTP 200 承载业务错误 code={code}"
                        f"（按 {status_code} 处置）：{' '.join(text.split())[:160]}",
                    )

            if status_code >= 400:
                _drop_gate()  # 本趟上游尝试已结束；continue 的下一趟会重新申请
                if text is None:
                    text = (await resp.aread()).decode("utf-8", "ignore")
                    await cm.__aexit__(None, None, None)
                    await client.aclose()

                # 验证码挑战：三形态任一命中即清池重试（不改账号状态）。zcode-plan
                # 渠道上即使这一趟没带码也要探测——若上游仍要求验证码，下一趟补码重试。
                challenge = _detect_captcha_challenge(resp, text) if channel == "zai" else None
                if challenge:
                    captcha_manager.invalidate(_messages_egress())
                    captcha_retries += 1
                    if captcha_retries >= MAX_CAPTCHA_RETRIES:
                        account.record_result(False, "验证码挑战连续失败")
                        logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
                        return _NEXT_ACCOUNT
                    if not needs_captcha:
                        # 按配置本不该带码，但上游明确要码 → 下一趟补码
                        needs_captcha = True
                        logs.warn(req_id, f"账号 {account.name} 验证码挑战（{challenge}），补码重试")
                    else:
                        logs.warn(req_id, f"账号 {account.name} 验证码挑战（{challenge}），刷新重试")
                    continue  # 同账号重建请求重试

                # 边缘封禁（IP 级 405）：全进程暂停 plan 渠道，不惩罚账号。
                # 与下方账号级 3012 严格区分：换账号解决不了 IP 被封，只会把
                # 其余账号一起送进同一个封禁窗口。
                if _is_edge_block(status_code, text):
                    gate.trip_edge()
                    wait = gate.retry_after()
                    snippet = " ".join(text.split())[:200]
                    account.record_result(False, f"边缘封禁 HTTP {status_code}（IP 级，非账号）")
                    store.update_account(account)
                    logs.err(
                        req_id,
                        f"上游边缘封禁（IP 级），全池暂停 plan 渠道 {wait}s"
                        f"（账号 {account.name} 本身不改状态）；上游body={snippet!r}",
                    )
                    if _jump_to_fallback():
                        logs.warn(req_id, f"账号 {account.name} 边缘封禁，切 API Key 回退")
                        needs_captcha = False
                        continue
                    # hold 已触发：其余 plan 账号会被 acquire 本地拒绝，交给 dispatch 兜底
                    return _NEXT_ACCOUNT

                # 风控（3012「unusual activity」/ 405）：指数退避冷却自动恢复，累计
                # RISK_BAN_STRIKES 次升级禁用（人工恢复）。不立即重试（避免对风控
                # 账号持续施压）；冷却期零上游流量（is_cooling 全通道门禁）。
                if _is_risk_control(status_code, text):
                    # 405 一律按风控计（RISK_CONTROL_HTTP_STATUSES），所以必须把上游
                    # body 留一段在日志里——否则分不清是 3012 标记、验证码挑战还是
                    # WAF/签名拒绝，strikes 花完了仍不知道上游在拒什么。
                    snippet = " ".join(text.split())[:200]
                    account.record_result(False, f"风控 HTTP {status_code}（3012/unusual activity）")
                    account.risk_penalty(
                        settings.RISK_COOLDOWN_BASE, settings.RISK_COOLDOWN_MAX,
                        settings.RISK_BAN_STRIKES, settings.RISK_STRIKE_DECAY_SECONDS,
                    )
                    if account.status == Status.DISABLED:
                        account.last_error = (
                            f"风控封禁 (3012/unusual activity) HTTP {status_code}，"
                            f"确认恢复后请在后台手动启用（第 {account.risk_strikes} 次）"
                        )
                        penalty_note = f"已升级禁用（第 {account.risk_strikes} 次）"
                        if settings.RISK_AUTO_ROTATE:
                            # 「风控后换设备重生」自动化：升级禁用即换发全新设备
                            # （新 SKU + 新 device_mid），后台补跑安装序；账号重新
                            # 启用时已是全新身份（billing/claim 头同源跟随档案）
                            from ..fingerprint import profile_for, rotate
                            from ..install import schedule_install

                            old_mid = profile_for(account).device_mid[:8]
                            rotate(account)
                            account.installed_at = None
                            schedule_install(account)
                            logs.warn(
                                req_id,
                                f"账号 {account.name} 风控禁用，已自动换发设备指纹"
                                f"（旧 mid {old_mid}）",
                            )
                    else:
                        cool_until = time.strftime(
                            "%H:%M", time.localtime(account.cooling_until)
                        ) if account.cooling_until else "?"
                        account.last_error = (
                            f"命中风控 (3012/unusual activity) HTTP {status_code}，"
                            f"指数退避冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                        )
                        penalty_note = f"冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                    store.update_account(account)
                    if _jump_to_fallback():
                        logs.warn(
                            req_id,
                            f"账号 {account.name} 命中风控 HTTP {status_code}（出口={captcha_egress}），"
                            f"{penalty_note}，切 API Key 回退；上游body={snippet!r}",
                        )
                        needs_captcha = False
                        continue
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 命中风控 HTTP {status_code}（出口={captcha_egress}），"
                        f"{penalty_note}，切换下一个；上游body={snippet!r}",
                    )
                    return _NEXT_ACCOUNT

                if _is_exhausted(status_code, text):
                    account.record_result(False, f"额度用完 HTTP {status_code}")
                    if account.status in (Status.INVALID, Status.DISABLED):
                        store.update_account(account)
                    else:
                        reset_ts = _parse_exhaust_reset(text)
                        if reset_ts:
                            account.cooling_until = reset_ts
                            account.last_error = (
                                f"额度已用完（重置于 "
                                f"{time.strftime('%m-%d %H:%M', time.localtime(reset_ts))}）"
                            )
                            account.status = Status.EXHAUSTED
                            store.update_account(account)
                        else:
                            _mark(account, Status.EXHAUSTED, "额度已用完")
                        _spawn_bg(_safe_refresh(account))
                    logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                    return _NEXT_ACCOUNT

                if status_code == 401:
                    account.record_result(False, "鉴权失败 HTTP 401")
                    _mark(account, Status.INVALID, "鉴权失败 HTTP 401")
                    if _jump_to_fallback():
                        logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切 API Key 回退")
                        needs_captcha = False
                        continue
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切换下一个")
                    return _NEXT_ACCOUNT

                if status_code == 403:
                    # 403 已排除挑战形态（上方 challenge 分支），此处为真实鉴权拒绝
                    account.record_result(False, "鉴权失败 HTTP 403")
                    _mark(account, Status.INVALID, "鉴权失败 HTTP 403")
                    if _jump_to_fallback():
                        logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切 API Key 回退")
                        needs_captcha = False
                        continue
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切换下一个")
                    return _NEXT_ACCOUNT

                if status_code == 429:
                    # 1113「无余额/无资源包」是 Key 的永久状态（api.z.ai 实测秒回），
                    # 原地等待重试毫无意义，只会把客户端吊死在 RETRY_429_TIMES×WAIT 里
                    # ——快速换号/失败。
                    if "1113" in text or "insufficient balance" in text.lower():
                        account.record_result(False, "429 1113 无余额（Key 无资源包，永久态）")
                        logs.warn(req_id, f"账号 {account.name} 429 为 1113 无余额，跳过等待重试")
                        if _jump_to_fallback():
                            needs_captcha = False
                            retries_429 = 0
                            continue
                        return _NEXT_ACCOUNT
                    # 1302/1303「真限流」（Z.ai 频控，magpie 同源实证）：与并发争用
                    # 相反，继续原地重试只会加重——账号休息 RATE_LIMIT_COOLDOWN 秒
                    #（与 3012 同语义：休息期间本请求下沉 Key 渠道完成；无 Key 则换号）
                    if _is_rate_limit_hit(text):
                        cooldown = settings.RATE_LIMIT_COOLDOWN
                        account.record_result(False, f"429 code 1302/1303 真限流，休息 {cooldown}s")
                        if account.status not in (Status.INVALID, Status.DISABLED) and not account.is_cooling():
                            account.status = Status.COOLING
                            account.cooling_until = time.time() + cooldown
                            account.last_error = f"429 真限流（1302/1303），休息 {cooldown}s"
                        store.update_account(account)
                        if _jump_to_fallback():
                            logs.warn(
                                req_id,
                                f"账号 {account.name} 429 真限流（1302/1303），休息 {cooldown}s，"
                                f"切 API Key 回退",
                            )
                            needs_captcha = False
                            retries_429 = 0
                            continue
                        logs.warn(
                            req_id,
                            f"账号 {account.name} 429 真限流（1302/1303），休息 {cooldown}s，切换下一个",
                        )
                        return _NEXT_ACCOUNT
                    # 频控不是账号故障：不冷却，原地等一等再试，耗尽后换号且账号保持可用。
                    # Plan 系渠道耗尽 ≠ apikey 也耗尽：同账号切回退并归还该渠道的重试预算
                    #（与上方 3012/401/403 切回退同一语义）
                    # 并发族（3008/3009/3010）=「这一轮没排到」的瞬态争用，不是频控
                    concurrency_hit = _is_concurrency_hit(text)

                    if retries_429 < settings.RETRY_429_TIMES:
                        retries_429 += 1
                        # 3008/3009/3010「并发已满」族 = 并发争用，不是频控：上游不给
                        # Retry-After，槽位通常几秒内释放。等默认 60s 会把客户端白白
                        # 吊死。短退避 + 抖动打散重试。
                        wait = _parse_retry_after(resp.headers.get("retry-after"))
                        if wait is None:
                            if concurrency_hit:
                                wait = settings.RETRY_429_CONCURRENCY_WAIT + random.randint(0, 2)
                            else:
                                wait = settings.RETRY_429_WAIT
                        if concurrency_hit:
                            logs.warn(
                                req_id,
                                f"账号 {account.name} 模型并发已满（3008/3009/3010），{wait}s 后重试"
                                f"（{retries_429}/{settings.RETRY_429_TIMES}）",
                            )
                        else:
                            logs.warn(
                                req_id,
                                f"账号 {account.name} 被限流 429，{wait}s 后重试"
                                f"（{retries_429}/{settings.RETRY_429_TIMES}）",
                            )
                        _park_slot(slot_box)
                        _drop_gate()
                        await _sleep(wait)
                        if not _reacquire_slot(account, slot_box):
                            logs.warn(req_id, f"账号 {account.name} 429 等待后并发已满，切换下一个")
                            return _NEXT_ACCOUNT
                        continue
                    if _jump_to_fallback():
                        account.record_result(False, "Plan 通道 429 耗尽，切 API Key 回退")
                        logs.warn(
                            req_id,
                            f"账号 {account.name} Plan 通道 429 耗尽，切 API Key 回退",
                        )
                        needs_captcha = False
                        continue
                    if _is_concurrency_hit(text):
                        # 请求最终因「并发已满」放弃 Plan 渠道 = 真饱和信号（magpie
                        # busyCap 口径）；重试中途自愈的不计——突发抖动不该触发全池停摆
                        gate.note_busy()
                    account.record_result(False, f"429 重试 {settings.RETRY_429_TIMES} 次耗尽")
                    store.update_account(account)
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
                        _park_slot(slot_box)
                        _drop_gate()
                        await _sleep(settings.RETRY_5XX_WAIT)
                        if not _reacquire_slot(account, slot_box):
                            logs.warn(req_id, f"账号 {account.name} 5xx 等待后并发已满，切换下一个")
                            return _NEXT_ACCOUNT
                        continue
                    account.record_result(False, f"HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却")
                    if account.status in (Status.INVALID, Status.DISABLED) or account.is_cooling():
                        # Key 回退 5xx 不得覆盖废 JWT / 风控禁用，否则冷却结束会重开 Plan；
                        # 风控冷却同理（冷却期不覆盖，避免缩短风控退避）
                        store.update_account(account)
                        logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，Plan 已停用，切换下一个")
                    else:
                        cool = settings.COOLING_SECONDS
                        account.status = Status.COOLING
                        account.cooling_until = time.time() + cool
                        account.last_error = f"上游 HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却"
                        store.update_account(account)
                        logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，冷却 {cool}s，切换下一个")
                    return _NEXT_ACCOUNT

                # 其它 4xx：直接回传客户端；响应体全量落日志供排查
                # （错误 JSON 通常很小；防御性上限 4KB，超长按 HTML 类 WAF 页处理只留头部）
                account.fail_count += 1
                account.record_result(False, f"HTTP {status_code}: {text[:120]}".replace("\n", " "))
                store.update_account(account)
                logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
                body_log = text if len(text) <= 4000 else text[:4000] + f"...(共 {len(text)} 字节，疑似 WAF 页)"
                logs.warn(req_id, f"上游 {status_code} 完整响应体: {body_log}")
                reqlog.finish_error(req_id, f"HTTP {status_code}: {text[:120]}".replace("\n", " "),
                                    status=status_code, t_first=time.time() - attempt_t0)
                return JSONResponse(
                    _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                    status_code=status_code,
                )

            # 成功：记录用量并把打开的上游流交给调用方
            account.use_count += 1
            account.last_used_at = time.time()
            account.record_result(True, f"HTTP 200 · {model_name} · {time.time() - attempt_t0:.1f}s")
            # API Key 回退成功不得把废 JWT / 风控禁用洗成 active，也不得清风控计数；
            # 风控冷却未到期同样不洗（上游风控标记为小时级粘性，回退通道成功不代表
            # Plan 通道已解除，洗掉会立刻重打 Plan 并累加计数）。冷却已过期后的
            # Plan 通道成功即证明恢复，全量清理（清风控计数）。
            if account.status not in (Status.INVALID, Status.DISABLED) and not account.is_cooling():
                account.risk_strikes = 0
                account.last_risk_at = None
                account.last_error = None
                account.cooling_until = None
                if account.status in (Status.COOLING, Status.EXHAUSTED):
                    account.status = Status.ACTIVE
            store.update_account(account)
            _spawn_bg(_safe_refresh(account))

            # 所有权先摘再随流转移：finally 只在「未转移」时归还，否则成功关闭时
            # 会二次 release，错减其它请求的闸门槽位
            gate_release = gate.release if gate_acquired else None
            gate_acquired = False
            return _Upstream(resp, cm, client, t_first=time.time() - attempt_t0,
                             account_name=account.name, mode=account.mode,
                             gate_release=gate_release)
    finally:
        # 所有权未转移（gate_acquired 仍 True）= 本趟尝试异常/提前退出，
        # 归还闸门槽位；成功路径已在 return 前摘除，不会二次归还
        _drop_gate()


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        live = store.find(account.provider, account.id)
        if live is None:
            return
        if live.provider == "zai" and live.allows_billing():
            # 去抖：每条消息都刷 billing 是流量放大器（会加剧风控），与 monitor 共享
            # last_checked_at，最小间隔内的刷新直接跳过
            last = live.last_checked_at
            if last and time.time() - last < settings.BILLING_REFRESH_MIN_INTERVAL:
                return
            await fetch_quota(live)
    except Exception:  # noqa: BLE001
        pass
