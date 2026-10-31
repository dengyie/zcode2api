"""GW-012 /v1/responses（OpenAI Responses 风格）端点集成测试。

复用 /v1/messages 同一套 mock 上游（返回 Anthropic 格式），验证双向转换：
非流式 response 对象、流式 response.* 事件帧，以及 previous_response_id
跨轮续接（response_store 链入历史）。
"""

from __future__ import annotations

import json

import pytest

# JWT 前缀是 mock 上游按 bind 键控 counters 的依据（session 级共享），
# 不能与既有测试文件的凭证前缀撞车（connect_fail 的 n==0 判定会被破坏）。
_GOOD_JWT = "hZ.eyJzdWIiOiJ6In0.sig"


@pytest.mark.integration
class TestResponsesEndpoint:
    async def test_nonstream_basic(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="resp")
        res = await client.post(
            "/v1/responses",
            json={"model": "GLM-5.3", "input": "你好"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["object"] == "response"
        assert data["id"].startswith("resp_")
        assert data["status"] == "completed"
        assert data["model"] == "GLM-5.3"
        msg_items = [o for o in data["output"] if o["type"] == "message"]
        assert msg_items, "output 应含 message 条目"
        texts = [p["text"] for p in msg_items[0]["content"] if p["type"] == "output_text"]
        assert "Hello from mock upstream" in "".join(texts)
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5

        # 上游收到的 body：messages[0] 是 user 文本、model 正确
        payload = json.loads(mock.state.calls[-1][3])
        assert payload["model"] == "GLM-5.3"
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"][0]["type"] == "text"
        assert payload["messages"][0]["content"][0]["text"] == "你好"

    async def test_stream_emits_response_events(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="resp-stream")
        res = await client.post(
            "/v1/responses",
            json={"model": "glm-5.3", "stream": True, "input": "hi"},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert "event: response.created" in res.text
        assert "event: response.completed" in res.text
        assert "[DONE]" not in res.text  # Responses 流没有 [DONE] 哨兵
        # 上游收到流式标记
        payload = json.loads(mock.state.calls[-1][3])
        assert payload["stream"] is True

    async def test_previous_response_id_chain(self, gateway_client, fresh_app):
        """两轮对话：第二轮带 previous_response_id，上游应收到完整历史重放。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="resp-chain")
        r1 = await client.post("/v1/responses",
                               json={"model": "GLM-5.3", "input": "第一轮问题"})
        assert r1.status_code == 200
        resp_id = r1.json()["id"]

        r2 = await client.post(
            "/v1/responses",
            json={"model": "GLM-5.3", "input": "第二轮问题",
                  "previous_response_id": resp_id},
        )
        assert r2.status_code == 200
        assert r2.json()["id"] != resp_id
        payload = json.loads(mock.state.calls[-1][3])
        msgs = payload["messages"]
        # 第一轮 user + 第一轮 assistant 输出 + 第二轮 user
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"][0]["text"] == "第一轮问题"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"][0]["text"] == "Hello from mock upstream"
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"][0]["text"] == "第二轮问题"

    async def test_invalid_payload_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post("/v1/responses", json={"input": "hi"})  # 缺 model
        assert res.status_code == 400
        assert res.json()["error"]["type"] == "invalid_request_error"

    async def test_unknown_previous_response_id_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post(
            "/v1/responses",
            json={"model": "GLM-5.3", "input": "hi",
                  "previous_response_id": "resp_does_not_exist"},
        )
        assert res.status_code == 400
        assert res.json()["error"]["type"] == "invalid_request_error"

    async def test_no_account_returns_503(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/responses",
                                    json={"model": "glm-5.3", "input": "hi"})
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"

    async def test_gateway_key_required(self, gateway_client, fresh_app):
        client, _ = gateway_client
        fresh_app.set_setting("gateway_key", "sk-gw-test")
        try:
            res = await client.post("/v1/responses",
                                    json={"model": "glm-5.3", "input": "hi"})
            assert res.status_code == 401
            res = await client.post("/v1/responses",
                                    headers={"x-api-key": "sk-gw-test"},
                                    json={"model": "glm-5.3", "input": "hi"})
            assert res.status_code == 503  # 鉴权通过，进入调度（无账号）
        finally:
            fresh_app.set_setting("gateway_key", "")
