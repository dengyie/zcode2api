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
        # 最近请求明细：{ok, at, detail} 形态，悬停 tooltip 数据源
        acc = next(a for a in fresh_app.list_accounts("zai") if a.name == "oai")
        last = acc.recent_results[-1]
        assert isinstance(last, dict) and last["ok"] is True
        assert "HTTP 200" in last["detail"] and last["at"] > 0

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
        # 严格三块骨架（cli_prefix / 身份+Harness / 动态尾块）：
        # 外来 system 文本只许并入末块内部，追加独立块会触发上游 3012
        assert len(payload["system"]) == 3
        assert "守则" in payload["system"][-1]["text"]

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

    async def test_max_tokens_string_form_clamped(self, gateway_client, fresh_app):
        """字符串型 max_tokens 也要钳制，防止绕过类型检查打到上游 1210。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt-str")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": "999999",
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


@pytest.mark.integration
class TestResponsesEndpoint:
    """/v1/responses：入站 Responses → chat → anthropic，出站反向翻译。"""

    async def test_nonstream_text(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="resp")
        res = await client.post("/v1/responses", json={
            "model": "glm-5.3-flash",
            "instructions": "只答要点",
            "input": "hi",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["object"] == "response"
        assert data["status"] == "completed"
        assert data["output_text"] == "Hello from mock upstream"
        assert data["output"][0]["content"][0]["type"] == "output_text"
        # instructions 落到上游 system（并入末块，仍保持严格三块骨架）
        payload = json.loads(mock.state.calls[-1][3])
        assert len(payload["system"]) == 3
        assert "只答要点" in payload["system"][-1]["text"]

    async def test_stream_event_names(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="resp-stream")
        res = await client.post("/v1/responses", json={
            "model": "glm-5.3-flash", "input": "hi", "stream": True,
        })
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [ln[7:].strip() for ln in res.text.splitlines() if ln.startswith("event: ")]
        assert events[0] == "response.created"
        assert "response.output_text.delta" in events
        assert events[-1] == "response.completed"
        datas = [json.loads(ln[6:]) for ln in res.text.splitlines() if ln.startswith("data: ")]
        seqs = [d["sequence_number"] for d in datas]
        assert seqs == list(range(len(seqs)))  # 从 0 起严格连续（对齐 Responses SSE）
        text = "".join(d["delta"] for d in datas if d["type"] == "response.output_text.delta")
        assert "chunk-0" in text

    async def test_previous_response_id_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post("/v1/responses", json={
            "model": "glm-5.3", "input": "hi", "previous_response_id": "resp_x",
        })
        assert res.status_code == 400
        assert "previous_response_id" in res.json()["error"]["message"]

    async def test_no_account_returns_503(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/responses", json={"model": "glm-5.3", "input": "hi"})
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"


@pytest.mark.integration
class TestCountTokensEndpoint:
    """count_tokens：本地估算，不打上游、不需要可用账号。"""

    async def test_estimate_without_account(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/messages/count_tokens", json={
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            })
        assert res.status_code == 200
        assert res.json()["input_tokens"] >= 1

    async def test_scales_with_content(self, gateway_client, fresh_app):
        client, mock = gateway_client
        before = len(mock.state.calls)
        small = (await client.post("/v1/messages/count_tokens", json={
            "model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}],
        })).json()["input_tokens"]
        big = (await client.post("/v1/messages/count_tokens", json={
            "model": "glm-5.3", "messages": [{"role": "user", "content": "长" * 500}],
        })).json()["input_tokens"]
        assert big > small
        assert len(mock.state.calls) == before  # 完全不打上游

    async def test_missing_model_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post("/v1/messages/count_tokens",
                                json={"messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 400
