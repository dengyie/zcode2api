"""GW-011 /v1/chat/completions（OpenAI 风格）端点集成测试。

复用 /v1/messages 同一套 mock 上游（返回 Anthropic 格式），验证双向转换：
非流式 JSON 与流式 SSE chunk。
"""

from __future__ import annotations

import json

import pytest

_GOOD_JWT = "hO.eyJzdWIiOiJvIn0.sig"


@pytest.mark.integration
class TestChatCompletions:
    async def test_nonstream_basic(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.3-flash",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "GLM-5.3-Flash"
        assert data["choices"][0]["message"]["content"] == "Hello from mock upstream"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 10

    async def test_system_message_reaches_upstream_as_system(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-sys")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "GLM-5.3",
                  "messages": [{"role": "system", "content": "守则"},
                               {"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = json.loads(mock.state.calls[-1][3])
        assert payload["system"][-1]["text"] == "守则"  # 用户 system 位于官方身份块之后

    async def test_max_tokens_clamped_to_upstream_limit(self, gateway_client, fresh_app):
        """上游 400 code 1210：max_tokens 上限 131072，网关钳制（GW-012）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": 999999,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = json.loads(mock.state.calls[-1][3])
        assert payload["max_tokens"] == 131072

    async def test_max_tokens_floor_and_passthrough(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt2")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": 1024,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = json.loads(mock.state.calls[-1][3])
        assert payload["max_tokens"] == 1024  # 合法值原样透传

    async def test_stream_returns_openai_chunks(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-stream")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.3-flash", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in res.text.splitlines() if ln.startswith("data: ")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert "chunk-0" in text
        finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"][0]["finish_reason"]]
        assert finishes == ["stop"]

    async def test_invalid_payload_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post("/v1/chat/completions",
                                json={"messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 400
        assert res.json()["error"]["type"] == "invalid_request_error"

    async def test_no_account_returns_503(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/chat/completions",
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"

    async def test_gateway_key_required(self, gateway_client, fresh_app):
        client, _ = gateway_client
        fresh_app.set_setting("gateway_key", "sk-gw-test")
        try:
            res = await client.post("/v1/chat/completions",
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
            assert res.status_code == 401
            res = await client.post("/v1/chat/completions",
                                    headers={"x-api-key": "sk-gw-test"},
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
            assert res.status_code == 503  # 鉴权通过，进入调度（无账号）
        finally:
            fresh_app.set_setting("gateway_key", "")
