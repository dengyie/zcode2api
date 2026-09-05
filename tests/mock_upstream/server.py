"""Mock 上游 —— zcode-hub 的核心测试资产（docs/testing/04 §2 故障注入矩阵）。

一个 FastAPI 应用，模拟 zcode.z.ai / api.z.ai 的全部被依赖端点：
  POST /api/v1/zcode-plan/anthropic/v1/messages   Plan 通道（JWT）
  POST /api/anthropic/v1/messages                 API Key 通道
  GET  /api/v1/zcode-plan/billing/current|balance|usage|preview
  POST /api/v1/zcode-plan/billing/claim           套餐领取（需验证码头）
  GET  /api/v1/client/configs                     验证码配置（公开）
  POST /api/v1/oauth/cli/init, GET poll/{id}      OAuth CLI 流程

控制协议（请求头，docs 04 §2）：
  x-mock-scenario: <name>        本请求的故障场景（默认 ok）
  x-mock-sequence: s1,s2,...     按该凭证的请求次数依序消费，耗尽后保持最后一个
  x-mock-bind: <凭证前缀>        与凭证绑定；网关不会带这个头，测试侧先注入到
                                 账号的上游头里即可实现「按账号注入」
  x-mock-sse-chunks / x-mock-sse-truncate-at: SSE 形态控制

观测协议（响应头）：
  x-mock-call-index: <n>         该凭证第 n 次被调用
记录：app.state.calls —— [(method, path, headers, body_bytes)]，测试直接断言。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request, Response

SSE_EVENT = (
    'event: content_block_delta\ndata: {{"type":"content_block_delta",'
    '"index":0,"delta":{{"type":"text_delta","text":"{text}"}}}}\n\n'
)
SSE_DONE = (
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}'
    ',"usage":{"output_tokens":5}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


_MESSAGES_PATHS = {
    "/api/v1/zcode-plan/anthropic/v1/messages",
    "/api/anthropic/v1/messages",
}


_DISCONNECT = object()  # 非 None 哨兵：见 _wrap_asgi docstring


def _wrap_asgi(app):
    """真断连实现（connect_fail_first）。

    断连语义只能发生在 FastAPI 之外：ServerErrorMiddleware 会把端点异常兜底成
    500 响应，内层无法表达"连接中断"。因此这里在最外层直接读 scope 头做判定
    （bind/sequence 解析与端点 _messages 重复，有意为之），命中"首次失败"时不调
    用 app，返回非 None 哨兵 —— uvicorn 的 run_asgi 对「未开始响应 + 返回值非
    None」的处理是 transport.close()（protocols/http/httptools_impl.py），连接上
    不写任何响应字节。客户端（真 TCP）拿到的是连接级错误（httpx 的
    RemoteProtocolError/ConnectError 族），与上游真实断连同形，网关的
    httpx.HTTPError 分支才能被正确触发。

    注意：命中时端点不执行，app.state.calls 不记录这次调用（计数仍 +1）。
    """

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in _MESSAGES_PATHS:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            bind = headers.get("x-mock-bind") or (headers.get("authorization", "")
                                                  .removeprefix("Bearer ") or
                                                  headers.get("x-api-key", ""))[:16] or "anonymous"
            scenario = headers.get("x-mock-scenario")
            state = app.state
            n = state.counters.get(bind, 0)
            if not scenario:
                seq = state.sequences.get(bind)
                if seq:
                    scenario = seq[min(n, len(seq) - 1)]
            if scenario == "connect_fail_first" and n == 0:
                state.counters[bind] = 1
                return _DISCONNECT
        await app(scope, receive, send)

    class _Wrapped:
        # 测试侧直接拿 `app.state` 断言（conftest fixture 返回的是包装对象），
        # 因此包装对象把属性访问转发给内层 FastAPI 实例。
        def __init__(self, inner, asgi):
            self._inner = inner
            self._asgi = asgi

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def __call__(self, scope, receive, send):
            # 必须 return：wrapped 的 _DISCONNECT 哨兵（非 None）正是 uvicorn
            # transport.close() 的触发条件，吞掉返回值会退化成 500 兜底。
            return await self._asgi(scope, receive, send)

    return _Wrapped(app, wrapped)


# scenario → (status, body-builder)。独立函数便于维护矩阵。
def _error_body(message: str, **extra: Any) -> dict:
    return {"error": {"message": message, "type": extra.pop("type", "upstream_error")}}


def build_app() -> FastAPI:
    app = FastAPI(title="mock-upstream")
    app.state.calls: list[tuple[str, str, dict, bytes]] = []
    app.state.counters: dict[str, int] = {}      # 凭证前缀 → 调用次数
    app.state.sequences: dict[str, list[str]] = {}  # bind → scenario 队列（测试侧写入）

    def _record(method: str, path: str, headers: dict, body: bytes) -> None:
        app.state.calls.append((method, path, headers, body))

    def _bind_key(headers: dict) -> str:
        auth = headers.get("authorization") or ""
        api_key = headers.get("x-api-key") or ""
        cred = auth.removeprefix("Bearer ") or api_key
        return cred[:16] or "anonymous"

    def _scenario_for(headers: dict, bind: str) -> str:
        explicit = headers.get("x-mock-scenario")
        if explicit:
            return explicit
        seq = app.state.sequences.get(bind)
        if seq:
            idx = min(app.state.counters.get(bind, 0), len(seq) - 1)
            return seq[idx]
        return "ok"

    async def _messages(request: Request, plan_channel: bool) -> Response:
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        _record("POST", request.url.path, headers, body)
        bind = headers.get("x-mock-bind") or _bind_key(headers)
        n = app.state.counters.get(bind, 0)
        app.state.counters[bind] = n + 1
        scenario = _scenario_for(headers, bind)

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        stream = bool(payload.get("stream"))

        # connect_fail_first 不在这里处理：判定在 _wrap_asgi 最外层
        #（FastAPI 的 ServerErrorMiddleware 会把异常兜底成 500，无法表达断连语义）

        if scenario == "slow_first_byte":
            await asyncio.sleep(30)

        status, resp_body, extra_headers = _messages_result(scenario, payload)
        if status != 200:
            return Response(
                json.dumps(resp_body), status_code=status,
                media_type="application/json",
                headers={"x-mock-call-index": str(n), **extra_headers},
            )

        if stream:
            chunks = int(headers.get("x-mock-sse-chunks", 3))
            truncate_at = headers.get("x-mock-sse-truncate-at")
            content = _sse_stream(chunks)
            if scenario == "sse_truncate" and truncate_at is not None:
                cut = int(truncate_at)
                content = content[:cut]
            elif scenario == "sse_truncate":
                content = content[: len(content) // 2]
            return Response(
                content, status_code=200, media_type="text/event-stream",
                headers={"x-mock-call-index": str(n), "cache-control": "no-cache"},
            )

        media = "text/html" if scenario == "garbage_body" else "application/json"
        raw = resp_body if isinstance(resp_body, str) else json.dumps(resp_body)
        return Response(raw, status_code=200, media_type=media,
                        headers={"x-mock-call-index": str(n)})

    def _messages_result(scenario: str, payload: dict) -> tuple[int, Any, dict]:
        """返回 (status, body, extra_headers)。"""
        model = payload.get("model", "GLM-5.2")
        mid = payload.get("id") or "msg_mock_001"
        ok_body = {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "Hello from mock upstream"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        if scenario == "quota_exhausted":
            return 402, _error_body("insufficient balance"), {}
        if scenario == "quota_exhausted_400":
            return 400, {"code": 1002, "message": "额度已用完"}, {}
        if scenario == "rate_limited":
            return 429, _error_body("rate limited"), {"retry-after": "30"}
        if scenario == "auth_invalid":
            return 401, _error_body("invalid api key", type="authentication_error"), {}
        if scenario == "captcha_challenge":
            return 403, _error_body("captcha verify failed"), {"x-aliyun-captcha-verify-param": "need"}
        if scenario == "captcha_3007":
            return 400, {"code": 3007, "message": "captcha required"}, {}
        if scenario == "risk_control_3012":
            # 2026-09-05 实测形态：HTTP 405 承载 {"code":3012,"msg":"...unusual activity..."}
            return 405, {"code": 3012, "msg": "request has been blocked due to unusual activity."}, {}
        if scenario == "server_error":
            return 500, {"error": "internal"}, {}
        if scenario == "not_found":
            return 404, _error_body("no such route", type="invalid_request_error"), {}
        if scenario == "garbage_body":
            return 200, "<html>not json</html>", {}
        return 200, ok_body, {}

    def _sse_stream(chunks: int) -> str:
        parts = [
            'event: message_start\ndata: {"type":"message_start","message":{"role":"assistant"}}\n\n'
        ]
        for i in range(chunks):
            parts.append(SSE_EVENT.format(text=f"chunk-{i}"))
        parts.append(SSE_DONE)
        return "".join(parts)

    async def _billing_current(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        _record("GET", request.url.path, headers, b"")
        if headers.get("x-mock-scenario") == "waf_block":
            return Response("<html>waf</html>", status_code=403, media_type="text/html")
        if headers.get("authorization", "").endswith("bad-jwt"):
            return Response(json.dumps(_error_body("invalid token")), status_code=401)
        return Response(json.dumps({
            "data": {"plans": [{
                "plan_id": "start", "show_name": "Start Plan",
                "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            }]},
        }), media_type="application/json")

    async def _billing_balance(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        _record("GET", request.url.path, headers, b"")
        if headers.get("x-mock-scenario") == "waf_block":
            return Response("<html>waf</html>", status_code=403, media_type="text/html")
        if headers.get("authorization", "").endswith("bad-jwt"):
            return Response(json.dumps(_error_body("invalid token")), status_code=401)
        return Response(json.dumps({
            "data": {"balances": [
                {"model": "GLM-5.3", "show_name": "GLM-5.3",
                 "total_units": 3000000, "used_units": 1000000, "remaining_units": 2000000,
                 "expires_at": "2099-01-01T00:00:00Z"},
                {"model": "GLM-5-Turbo", "show_name": "GLM-5-Turbo",
                 "total_units": 2000000, "used_units": 2000000, "remaining_units": 0,
                 "expires_at": "2099-01-01T00:00:00Z"},
            ]},
        }), media_type="application/json")

    async def _usage(request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        return Response(json.dumps({"data": {"requests": 42}}), media_type="application/json")

    async def _client_configs(request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        return Response(json.dumps({
            "data": {"configs": {"captcha": {
                "enabled": True, "prefix": "mockpre", "region": "sgp", "sceneId": "mock-scene",
            }}},
        }), media_type="application/json")

    async def _event_report(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        body = await request.body()
        _record("POST", request.url.path, headers, body)
        if getattr(app.state, "event_report_fail", None):
            return Response(json.dumps({"code": -1, "msg": app.state.event_report_fail}),
                            status_code=500, media_type="application/json")
        return Response(json.dumps({"code": 0}), media_type="application/json")

    # ── 套餐领取（claim）───────────────────────────────────────────────────
    _CLAIM_PLAN = {
        "plan_id": "mock-claim-plan",
        "name": "Mock Daily Plan",
        "description": "mock 每日赠送",
        "priority": 100,
        "entitlements": [
            {"entitlement_id": "e1", "show_name": "GLM-5.3", "meter": "model_usage",
             "unit_type": "token", "grant_units": 3000000, "period": "daily"},
            {"entitlement_id": "e2", "show_name": "噪音项", "meter": "other",
             "unit_type": "token", "grant_units": 1, "period": "daily"},
        ],
    }

    async def _billing_preview(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        _record("GET", request.url.path, headers, b"")
        scenario = getattr(app.state, "claim_scenario", None) or headers.get("x-mock-scenario")
        if scenario == "claim_none":
            plans: list = []
        elif scenario == "claim_claimed":
            return Response(json.dumps({"code": 1003, "msg": "already claimed"}),
                            media_type="application/json")
        elif scenario == "claim_expired":
            return Response(json.dumps({"code": 1002, "msg": "expired"}),
                            media_type="application/json")
        else:
            plans = [dict(_CLAIM_PLAN)]
        return Response(json.dumps({"code": 0, "data": {"plans": plans}}),
                        media_type="application/json")

    async def _billing_claim(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        body = await request.body()
        _record("POST", request.url.path, headers, body)
        if not headers.get("x-aliyun-captcha-verify-param"):
            return Response(json.dumps({"code": 3007, "msg": "captcha required"}),
                            media_type="application/json")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if not payload.get("plan_id"):
            return Response(json.dumps({"code": 3001, "msg": "bad plan"}),
                            media_type="application/json")
        scenario = getattr(app.state, "claim_scenario", None) or headers.get("x-mock-scenario")
        if scenario == "claim_captcha_fail":
            return Response(json.dumps({"code": 3007, "msg": "captcha invalid"}),
                            media_type="application/json")
        if scenario == "claim_claimed":
            return Response(json.dumps({"code": 1003, "msg": "already claimed"}),
                            media_type="application/json")
        return Response(json.dumps({"code": 0, "data": {"plan_id": payload["plan_id"]}}),
                        media_type="application/json")

    @app.post("/api/v1/oauth/cli/init")
    async def oauth_init(request: Request) -> Response:
        body = await request.body()
        _record("POST", request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
        n = app.state.oauth_init_count = getattr(app.state, "oauth_init_count", 0) + 1
        return Response(json.dumps({
            "data": {"flow_id": f"mock-flow-{n}", "authorize_url": "https://mock.example/authorize"}
        }), media_type="application/json")

    @app.get("/api/v1/oauth/cli/poll/{flow_id}")
    async def oauth_poll(flow_id: str, request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        # 测试协议：app.state.oauth_state = "ready" | "failed" | "pending"
        #（默认 pending；ready 时附带可兑换的 mock 凭证组）
        state = getattr(app.state, "oauth_state", "pending")
        data: dict = {"status": state}
        if state == "failed":
            data["message"] = getattr(app.state, "oauth_fail_message", "user denied")
        if state == "ready":
            data.update({
                "status": "ready",
                "token": "mock-gateway-jwt-header.eyJzdWIiOiJtb2NrIn0.sig",
                "zai": {"access_token": "mock-access-token"},
            })
        return Response(json.dumps({"data": data}), media_type="application/json")

    @app.post("/api/auth/z/login")
    async def z_login(request: Request) -> Response:
        body = await request.body()
        _record("POST", request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
        return Response(json.dumps({
            "data": {"access_token": "mock-biz-token"}
        }), media_type="application/json")

    @app.get("/api/biz/customer/getCustomerInfo")
    async def customer_info(request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        return Response(json.dumps({
            "data": {"organizations": [{
                "organizationId": "mock-org-1", "organizationName": "默认机构",
                "projects": [{"projectId": "mock-proj-1", "projectName": "默认项目"}],
            }]},
        }), media_type="application/json")

    @app.get("/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys")
    async def list_api_keys(org_id: str, proj_id: str, request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        return Response(json.dumps({"data": []}), media_type="application/json")

    @app.post("/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys")
    async def create_api_key(org_id: str, proj_id: str, request: Request) -> Response:
        body = await request.body()
        _record("POST", request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
        return Response(json.dumps({
            "data": {"apiKey": "mock-api-key-id"}
        }), media_type="application/json")

    @app.get("/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys/copy/{api_key}")
    async def copy_api_key(org_id: str, proj_id: str, api_key: str, request: Request) -> Response:
        _record("GET", request.url.path, {k.lower(): v for k, v in request.headers.items()}, b"")
        return Response(json.dumps({
            "data": {"secretKey": "mock-secret-key"}
        }), media_type="application/json")

    async def _messages_plan(request: Request) -> Response:
        return await _messages(request, plan_channel=True)

    async def _messages_api(request: Request) -> Response:
        return await _messages(request, plan_channel=False)

    app.post("/api/v1/zcode-plan/anthropic/v1/messages")(_messages_plan)
    app.post("/api/anthropic/v1/messages")(_messages_api)
    app.get("/api/v1/zcode-plan/billing/current")(_billing_current)
    app.get("/api/v1/zcode-plan/billing/balance")(_billing_balance)
    app.get("/api/v1/zcode-plan/billing/preview")(_billing_preview)
    app.post("/api/v1/zcode-plan/billing/claim")(_billing_claim)
    app.get("/api/v1/zcode-plan/usage")(_usage)
    app.get("/api/v1/client/configs")(_client_configs)
    app.post("/api/v1/event/report")(_event_report)

    return app


_fastapi_app = build_app()
app = _wrap_asgi(_fastapi_app)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5201, log_level="warning")
