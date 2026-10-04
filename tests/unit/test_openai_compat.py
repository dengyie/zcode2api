"""openai_compat 单元测试 —— OpenAI ↔ Anthropic 双向转换。"""

from __future__ import annotations

import json

from app.openai_compat import StreamConverter, anthropic_to_openai, openai_to_anthropic


class TestOpenaiToAnthropic:
    def test_basic_text_messages(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3-flash",
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
                {"role": "user", "content": "继续"},
            ],
        })
        assert err is None
        assert body["model"] == "glm-5.3-flash"
        assert body["system"] == "你是助手"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
        assert body["messages"][0]["content"] == [{"type": "text", "text": "你好"}]
        assert body["max_tokens"] == 4096  # OpenAI 可缺省，Anthropic 必填

    def test_developer_role_treated_as_system(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [
                {"role": "developer", "content": "规则"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert err is None
        assert body["system"] == "规则"

    def test_optional_params_mapping(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 128,
            "temperature": 0.5,
            "top_p": 0.9,
            "stop": ["END", "STOP"],
            "stream": True,
        })
        assert err is None
        assert body["max_tokens"] == 128
        assert body["temperature"] == 0.5
        assert body["top_p"] == 0.9
        assert body["stop_sequences"] == ["END", "STOP"]
        assert body["stream"] is True

    def test_single_stop_string(self):
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": "END",
        })
        assert body["stop_sequences"] == ["END"]

    def test_multimodal_image_data_url(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]}],
        })
        assert err is None
        blocks = body["messages"][0]["content"]
        assert blocks[0] == {"type": "text", "text": "看图"}
        assert blocks[1]["type"] == "image"
        assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}

    def test_remote_image_url_skipped(self):
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ]}],
        })
        blocks = body["messages"][0]["content"]
        assert len(blocks) == 1  # 外链图片无法回填，安静跳过

    def test_tool_calls_and_tool_result_roundtrip_shape(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [
                {"role": "user", "content": "天气如何"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"杭州\"}"},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "晴 25 度"},
            ],
            "tools": [{
                "type": "function",
                "function": {"name": "get_weather", "description": "查天气",
                             "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
            }],
            "tool_choice": "auto",
        })
        assert err is None
        asst = body["messages"][1]
        assert asst["content"][0] == {"type": "tool_use", "id": "call_1", "name": "get_weather",
                                      "input": {"city": "杭州"}}
        tool_msg = body["messages"][2]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "call_1"
        assert body["tools"][0] == {"name": "get_weather", "description": "查天气",
                                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}

    def test_tool_choice_variants(self):
        base = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f"}}]}
        body, _ = openai_to_anthropic({**base, "tool_choice": "required"})
        assert body["tool_choice"] == {"type": "any"}
        body, _ = openai_to_anthropic({**base, "tool_choice": {"type": "function", "function": {"name": "f"}}})
        assert body["tool_choice"] == {"type": "tool", "name": "f"}
        body, _ = openai_to_anthropic({**base, "tool_choice": "none"})
        assert "tools" not in body and "tool_choice" not in body

    def test_invalid_payloads(self):
        assert openai_to_anthropic({"messages": []})[1] is not None  # 缺 model
        assert openai_to_anthropic({"model": "glm-5.3"})[1] is not None  # 缺 messages
        assert openai_to_anthropic({"model": "glm-5.3", "messages": "x"})[1] is not None


class TestAnthropicToOpenai:
    def test_text_response(self):
        out = anthropic_to_openai({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "GLM-5.3",
            "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": " world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }, "GLM-5.3")
        assert out["object"] == "chat.completion"
        assert out["id"] == "msg_1"
        assert out["model"] == "GLM-5.3"
        choice = out["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": "Hello world"}
        assert choice["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_tool_use_response(self):
        out = anthropic_to_openai({
            "id": "msg_2", "type": "message", "model": "GLM-5.3",
            "content": [
                {"type": "text", "text": "查一下"},
                {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "杭州"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 8},
        }, "GLM-5.3")
        choice = out["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] == "查一下"
        assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"city": "杭州"}'

    def test_max_tokens_finish_reason(self):
        out = anthropic_to_openai({
            "id": "m", "type": "message", "content": [{"type": "text", "text": "x"}],
            "stop_reason": "max_tokens", "usage": {},
        }, "GLM-5.3")
        assert out["choices"][0]["finish_reason"] == "length"


def _parse_sse(s: str) -> list[dict]:
    return [json.loads(line[5:]) for line in s.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


class TestStreamConverter:
    def test_full_event_stream(self):
        conv = StreamConverter("GLM-5.3-Flash")
        outs = [conv.start()]
        events = [
            {"type": "message_start", "message": {"id": "msg_x", "usage": {"input_tokens": 7}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        for evt in events:
            outs.extend(conv.feed(evt))
        outs.append(conv.done())

        assert outs[-1] == "data: [DONE]\n\n"
        chunks = _parse_sse("".join(outs))
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        assert any(c["id"] == "msg_x" for c in chunks)  # 沿用上游 message id
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert text == "你好"
        finish_chunks = [c for c in chunks if c["choices"][0]["finish_reason"]]
        assert len(finish_chunks) == 1
        assert finish_chunks[0]["choices"][0]["finish_reason"] == "stop"
        assert finish_chunks[0]["usage"]["prompt_tokens"] == 7
        assert finish_chunks[0]["usage"]["completion_tokens"] == 2

    def test_tool_use_stream(self):
        conv = StreamConverter("GLM-5.3")
        conv.start()
        outs = []
        outs += conv.feed({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "tool_use", "id": "call_1", "name": "f"}})
        outs += conv.feed({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "input_json_delta", "partial_json": '{"a"'}})
        outs += conv.feed({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "input_json_delta", "partial_json": ":1}"}})
        chunks = _parse_sse("".join(outs))
        tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
        assert tcs[0]["id"] == "call_1" and tcs[0]["function"]["name"] == "f"
        assert "".join(t["function"]["arguments"] for t in tcs) == '{"a":1}'
        assert [t["index"] for t in tcs] == [0, 0, 0]

    def test_unknown_events_ignored(self):
        conv = StreamConverter("GLM-5.3")
        conv.start()
        assert conv.feed({"type": "ping"}) == []
        assert conv.feed({"type": "content_block_stop", "index": 0}) == []

class TestReasoningMapping:
    """reasoning 双向映射：reasoning_effort 入站透传 + thinking 块 ↔ reasoning_content 出站。"""

    def test_reasoning_effort_passthrough(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        })
        assert err is None
        assert body["reasoning_effort"] == "high"

    def test_reasoning_effort_normalizes_case(self):
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "MAX",
        })
        assert body["reasoning_effort"] == "max"

    def test_reasoning_effort_invalid_omitted(self):
        # 上游不支持关闭思考，off/none/未知值一律不带该字段
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "off",
        })
        assert "reasoning_effort" not in body

    def test_reasoning_effort_absent(self):
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert "reasoning_effort" not in body

    def test_assistant_reasoning_content_replays_as_thinking(self):
        body, err = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [
                {"role": "user", "content": "1+1"},
                {"role": "assistant", "content": "2", "reasoning_content": "用户问 1+1，答案是 2"},
                {"role": "user", "content": "再加 1"},
            ],
        })
        assert err is None
        assistant = body["messages"][1]
        assert [b["type"] for b in assistant["content"]] == ["thinking", "text"]
        assert assistant["content"][0]["thinking"] == "用户问 1+1，答案是 2"

    def test_assistant_reasoning_alias_fields(self):
        for alias in ("reasoning", "reasoning_text"):
            body, _ = openai_to_anthropic({
                "model": "glm-5.3",
                "messages": [
                    {"role": "assistant", "content": "ok", alias: "思考"},
                ],
            })
            assert body["messages"][0]["content"][0]["type"] == "thinking"

    def test_assistant_blank_reasoning_skipped(self):
        body, _ = openai_to_anthropic({
            "model": "glm-5.3",
            "messages": [{"role": "assistant", "content": "ok", "reasoning_content": "   "}],
        })
        # 空白思考不产生 thinking 块，也不丢 assistant 消息
        assert body["messages"][0]["content"] == [{"type": "text", "text": "ok"}]

    def test_nonstream_thinking_to_reasoning_content(self):
        data = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "glm-5.3",
            "content": [
                {"type": "thinking", "thinking": "先想一下", "signature": "sig"},
                {"type": "text", "text": "答案"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 9},
        }
        out = anthropic_to_openai(data, "glm-5.3")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "答案"
        assert msg["reasoning_content"] == "先想一下"

    def test_nonstream_no_thinking_no_field(self):
        data = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "glm-5.3",
            "content": [{"type": "text", "text": "答案"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 1},
        }
        out = anthropic_to_openai(data, "glm-5.3")
        assert "reasoning_content" not in out["choices"][0]["message"]

    def test_stream_thinking_to_reasoning_content(self):
        conv = StreamConverter("GLM-5.3")
        outs = [conv.start()]
        events = [
            {"type": "message_start", "message": {"id": "msg_r", "usage": {"input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "一下"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "答案"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
        ]
        for evt in events:
            outs.extend(conv.feed(evt))
        outs.append(conv.done())

        chunks = _parse_sse("".join(outs))
        reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or "" for c in chunks)
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert reasoning == "想一下"
        assert text == "答案"

    def test_stream_no_thinking_keeps_shape(self):
        # 原有纯文本流不应引入 reasoning_content 字段（与既有客户端行为兼容）
        conv = StreamConverter("GLM-5.3")
        conv.start()
        outs = conv.feed({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "你好"}})
        chunks = _parse_sse("".join(outs))
        assert all("reasoning_content" not in c["choices"][0]["delta"] for c in chunks)

