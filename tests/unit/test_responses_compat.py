"""responses_compat / response_store 单元测试 —— Responses ↔ Anthropic 双向转换。"""

from __future__ import annotations

import json
import re

from app import response_store
from app.responses_compat import (
    ResponsesStreamConverter,
    anthropic_to_responses,
    responses_to_anthropic,
)


class TestResponsesToAnthropic:
    def test_model_required(self):
        assert responses_to_anthropic({"input": "hi"})[1] is not None  # 缺 model
        assert responses_to_anthropic({"model": "", "input": "hi"})[1] is not None
        assert responses_to_anthropic({"model": 123, "input": "hi"})[1] is not None

    def test_string_input(self):
        body, err = responses_to_anthropic({"model": "GLM-5.3", "input": "你好"})
        assert err is None
        assert body["model"] == "GLM-5.3"
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
        assert body["max_tokens"] == 4096  # Responses 可缺省，Anthropic 必填

    def test_instructions_to_system(self):
        body, err = responses_to_anthropic({"model": "GLM-5.3", "instructions": "守则", "input": "hi"})
        assert err is None
        assert body["system"] == "守则"

    def test_user_and_assistant_message_items(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "问"}]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "答"}]},
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "再问"}]},
            ],
        })
        assert err is None
        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
        assert body["messages"][0]["content"] == [{"type": "text", "text": "问"}]
        assert body["messages"][1]["content"] == [{"type": "text", "text": "答"}]

    def test_unknown_role_treated_as_user(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "message", "role": "root",
                       "content": [{"type": "input_text", "text": "x"}]}],
        })
        assert body["messages"][0]["role"] == "user"

    def test_input_image_data_url(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]}],
        })
        assert err is None
        blocks = body["messages"][0]["content"]
        assert blocks[0] == {"type": "text", "text": "看图"}
        assert blocks[1]["type"] == "image"
        assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}

    def test_remote_image_url_skipped(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "https://example.com/a.png"},
            ]}],
        })
        blocks = body["messages"][0]["content"]
        assert len(blocks) == 1  # 外链图片无法回填，安静跳过

    def test_function_call_item_to_tool_use(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "天气如何"}]},
                {"type": "function_call", "call_id": "call_1", "name": "get_weather",
                 "arguments": "{\"city\":\"杭州\"}"},
            ],
        })
        assert err is None
        asst = body["messages"][1]
        assert asst["role"] == "assistant"
        assert asst["content"][0] == {"type": "tool_use", "id": "call_1", "name": "get_weather",
                                      "input": {"city": "杭州"}}

    def test_function_call_invalid_arguments_kept_as_raw(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "function_call", "call_id": "c1", "name": "f",
                       "arguments": "{bad json"}],
        })
        block = body["messages"][0]["content"][0]
        assert block["input"] == {"_raw": "{bad json"}

    def test_function_call_output_str(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": "晴 25 度"}],
        })
        msg = body["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"][0] == {"type": "tool_result", "tool_use_id": "call_1", "content": "晴 25 度"}

    def test_function_call_output_list_parts(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "output_text", "text": "第一段"},
                {"type": "output_text", "text": "第二段"},
            ]}],
        })
        assert body["messages"][0]["content"][0]["content"] == "第一段\n第二段"

    def test_reasoning_content_preferred_over_summary(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "完整思考"}],
                "summary": [{"type": "summary_text", "text": "摘要"}],
            }],
        })
        block = body["messages"][0]["content"][0]
        assert block == {"type": "thinking", "thinking": "完整思考", "signature": ""}

    def test_reasoning_summary_fallback(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "摘要思考"}],
            }],
        })
        assert body["messages"][0]["content"][0]["thinking"] == "摘要思考"

    def test_reasoning_empty_skipped(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
            ],
        })
        # 空 reasoning 不产生空 assistant 消息
        assert [m["role"] for m in body["messages"]] == ["user"]

    def test_adjacent_same_role_merged(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "一"}]},
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "二"}]},
            ],
        })
        # Anthropic 要求 role 交替，相邻同角色合并为一条消息
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"] == [{"type": "text", "text": "一"},
                                                  {"type": "text", "text": "二"}]

    def test_tools_flat_mapping(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "get_weather", "description": "查天气",
                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
                {"type": "web_search", "search_context_size": "low"},  # 内置类型跳过
            ],
        })
        assert err is None
        assert body["tools"] == [{"name": "get_weather", "description": "查天气",
                                  "input_schema": {"type": "object",
                                                   "properties": {"city": {"type": "string"}}}}]

    def test_tools_parameters_default_object(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3", "input": "hi",
            "tools": [{"type": "function", "name": "f"}],
        })
        assert body["tools"][0] == {"name": "f", "description": "", "input_schema": {"type": "object"}}

    def test_tool_choice_variants(self):
        base = {"model": "GLM-5.3", "input": "hi",
                "tools": [{"type": "function", "name": "f"}]}
        body, _ = responses_to_anthropic({**base, "tool_choice": "required"})
        assert body["tool_choice"] == {"type": "any"}
        body, _ = responses_to_anthropic({**base, "tool_choice": {"type": "function", "name": "f"}})
        assert body["tool_choice"] == {"type": "tool", "name": "f"}
        body, _ = responses_to_anthropic({**base, "tool_choice": "none"})
        assert "tools" not in body and "tool_choice" not in body
        body, _ = responses_to_anthropic({**base, "tool_choice": "auto"})
        assert "tool_choice" not in body

    def test_reasoning_effort_mapping(self):
        base = {"model": "GLM-5.3", "input": "hi"}
        for effort, expected in (("minimal", "low"), ("low", "low"),
                                 ("high", "high"), ("xhigh", "max")):
            body, _ = responses_to_anthropic({**base, "reasoning": {"effort": effort}})
            assert body["reasoning_effort"] == expected, effort
        # medium 即上游默认档：不带该字段（上游不支持关思考）
        body, _ = responses_to_anthropic({**base, "reasoning": {"effort": "medium"}})
        assert "reasoning_effort" not in body
        body, _ = responses_to_anthropic(base)
        assert "reasoning_effort" not in body

    def test_optional_params_mapping(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3", "input": "hi",
            "max_output_tokens": 128, "temperature": 0.5, "top_p": 0.9, "stream": True,
        })
        assert err is None
        assert body["max_tokens"] == 128
        assert body["temperature"] == 0.5
        assert body["top_p"] == 0.9
        assert body["stream"] is True

    def test_unknown_fields_quietly_ignored(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3", "input": "hi",
            "store": True, "include": [], "metadata": {"k": "v"},
            "prompt_cache_key": "ck", "text": {"format": {"type": "text"}},
        })
        assert err is None
        assert set(body.keys()) == {"model", "messages", "max_tokens"}

    def test_empty_input_rejected(self):
        assert responses_to_anthropic({"model": "GLM-5.3"})[1] == "必须提供 input"
        assert responses_to_anthropic({"model": "GLM-5.3", "input": []})[1] == "必须提供 input"

    def test_empty_input_with_previous_response_id_rejected(self):
        # 网关层命中历史后会把存储条目合并进 input，翻译器见到空 messages 一律早失败：
        # 空数组发上游只会换来语焉不详的 1214「messages 参数非法」
        body, err = responses_to_anthropic({"model": "GLM-5.3", "input": [],
                                            "previous_response_id": "resp_x"})
        assert err is not None and body is None

    def test_chat_shorthand_item_without_type(self):
        # 部分客户端的 input 数组发法：{"role":"user","content":"hi"}（无 type）——
        # 曾被当未知条目丢弃、发出空 messages 招致上游 400（实测 code 1214）
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "yo"}],
        })
        assert err is None
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        ]

    def test_chat_shorthand_parts_with_text_type(self):
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"role": "user",
                       "content": [{"type": "text", "text": "hi"}]}],
        })
        assert err is None
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]

    def test_empty_content_message_dropped(self):
        # 空内容消息无信息量，整条丢弃而非塞空 text 块（上游对空块同样敏感）
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "message", "role": "user", "content": ""},
                      {"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
        })
        assert err is None
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]

    def test_all_items_unusable_rejected(self):
        # 条目都映射不出内容 → 本地 400，不把空 messages 发给上游
        body, err = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": [{"type": "message", "role": "user", "content": ""},
                      {"type": "item_reference", "id": "msg_1"}],
        })
        assert err is not None and body is None

    def test_non_dict_items_skipped(self):
        body, _ = responses_to_anthropic({
            "model": "GLM-5.3",
            "input": ["garbage", 42, {"type": "item_reference", "id": "msg_1"},
                      {"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
        })
        assert [m["role"] for m in body["messages"]] == ["user"]


class TestAnthropicToResponses:
    def test_blocks_to_output_items(self):
        out = anthropic_to_responses({
            "id": "msg_1", "type": "message", "model": "GLM-5.3",
            "content": [
                {"type": "thinking", "thinking": "先想", "signature": "sig"},
                {"type": "text", "text": "答案"},
                {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "杭州"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }, "GLM-5.3")
        assert [o["type"] for o in out["output"]] == ["reasoning", "message", "function_call"]
        assert out["output"][0]["summary"] == [{"type": "summary_text", "text": "先想"}]
        assert out["output"][1]["role"] == "assistant"
        assert out["output"][1]["content"] == [{"type": "output_text", "text": "答案", "annotations": []}]
        assert out["output"][2]["call_id"] == "call_1"
        assert out["output"][2]["name"] == "get_weather"
        assert json.loads(out["output"][2]["arguments"]) == {"city": "杭州"}

    def test_envelope_shape(self):
        out = anthropic_to_responses({
            "id": "msg_1", "type": "message", "model": "GLM-5.3",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        }, "GLM-5.3")
        assert out["object"] == "response"
        assert out["id"].startswith("resp_")
        assert out["status"] == "completed"
        assert out["model"] == "GLM-5.3"
        assert out["error"] is None
        assert out["incomplete_details"] is None
        assert out["tool_choice"] == "auto"
        assert out["tools"] == []

    def test_max_tokens_incomplete(self):
        out = anthropic_to_responses({
            "id": "m", "type": "message", "content": [{"type": "text", "text": "x"}],
            "stop_reason": "max_tokens", "usage": {},
        }, "GLM-5.3")
        assert out["status"] == "incomplete"
        assert out["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_usage_conversion(self):
        out = anthropic_to_responses({
            "id": "m", "type": "message", "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
        }, "GLM-5.3")
        assert out["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 0},
        }

    def test_usage_missing_cache_defaults_zero(self):
        out = anthropic_to_responses({
            "id": "m", "type": "message", "content": [], "stop_reason": "end_turn", "usage": {},
        }, "GLM-5.3")
        assert out["usage"]["input_tokens_details"] == {"cached_tokens": 0}
        assert out["usage"]["total_tokens"] == 0


class TestResponsesStream:
    @staticmethod
    def _events():
        """手工构造的完整 Anthropic SSE 事件序列（思考 → 文本 → 工具调用）。"""
        return [
            {"type": "message_start", "message": {"id": "msg_x", "usage": {"input_tokens": 7}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "一下"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "你"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "好"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "call_1", "name": "f"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"a"'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ":1}"}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
            {"type": "message_stop"},
        ]

    def test_full_event_stream(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        frames = [conv.start()]
        for evt in self._events():
            frames.extend(conv.feed(evt))
        assert conv.done() == ""  # Responses 流没有 [DONE] 哨兵

        types = [json.loads(f.split("data: ", 1)[1])["type"]
                 for f in "".join(frames).split("\n\n") if f]
        assert types[0] == "response.created"
        assert types[1] == "response.in_progress"
        assert types[-1] == "response.completed"
        assert "response.output_item.added" in types and "response.output_item.done" in types

        # sequence_number 从 0 起严格递增
        seqs = [json.loads(f.split("data: ", 1)[1])["sequence_number"]
                for f in "".join(frames).split("\n\n") if f]
        assert seqs == list(range(len(seqs)))

    def test_text_delta_content(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        frames = []
        for evt in self._events():
            frames.extend(conv.feed(evt))
        raw = "".join(frames)
        deltas = [json.loads(m.group(1))["delta"]
                  for m in re.finditer(r"data: (.*)", raw)
                  if json.loads(m.group(1))["type"] == "response.output_text.delta"]
        assert "".join(deltas) == "你好"

    def test_function_call_arguments_done_accumulated(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        raw = "".join(f for evt in self._events() for f in conv.feed(evt))
        done_frames = [json.loads(m.group(1)) for m in re.finditer(r"data: (.*)", raw)
                       if json.loads(m.group(1))["type"] == "response.function_call_arguments.done"]
        assert len(done_frames) == 1
        assert done_frames[0]["arguments"] == '{"a":1}'

    def test_completed_snapshot(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        raw = "".join(f for evt in self._events() for f in conv.feed(evt))
        completed = [json.loads(m.group(1)) for m in re.finditer(r"data: (.*)", raw)
                     if json.loads(m.group(1))["type"] == "response.completed"]
        assert len(completed) == 1
        snap = completed[0]["response"]
        assert snap["status"] == "completed"
        assert snap["id"] == "resp_msg_x"  # 沿用上游 message id
        assert len(snap["output"]) == 3
        assert [o["type"] for o in snap["output"]] == ["reasoning", "message", "function_call"]
        assert snap["usage"]["input_tokens"] == 7
        assert snap["usage"]["output_tokens"] == 9
        assert snap["usage"]["total_tokens"] == 16

    def test_output_items_complete(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        for evt in self._events():
            conv.feed(evt)
        assert [o["type"] for o in conv.output_items] == ["reasoning", "message", "function_call"]
        assert conv.output_items[0]["summary"][0]["text"] == "想一下"
        assert conv.output_items[1]["content"][0]["text"] == "你好"
        assert json.loads(conv.output_items[2]["arguments"]) == {"a": 1}

    def test_max_tokens_incomplete_snapshot(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        events = [
            {"type": "message_start", "message": {"id": "msg_m", "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        raw = "".join(f for evt in events for f in conv.feed(evt))
        completed = [json.loads(m.group(1)) for m in re.finditer(r"data: (.*)", raw)
                     if json.loads(m.group(1))["type"] == "response.completed"]
        snap = completed[0]["response"]
        assert snap["status"] == "incomplete"
        assert snap["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_unknown_events_ignored(self):
        conv = ResponsesStreamConverter("GLM-5.3")
        conv.start()
        assert conv.feed({"type": "ping"}) == []
        assert conv.feed({"type": "content_block_stop", "index": 0}) == []  # 无在途块，安静忽略


class TestResponseStore:
    def setup_method(self):
        response_store.clear()

    def test_save_get_roundtrip(self):
        response_store.save("resp_a", [{"type": "message", "role": "user"}],
                            [{"type": "message", "role": "assistant"}])
        st = response_store.get("resp_a")
        assert st is not None
        assert st["input_items"] == [{"type": "message", "role": "user"}]
        assert st["output_items"] == [{"type": "message", "role": "assistant"}]
        assert response_store.size() == 1

    def test_miss_returns_none(self):
        assert response_store.get("resp_missing") is None

    def test_get_returns_shallow_copies(self):
        inp: list = [{"a": 1}]
        response_store.save("resp_b", inp, [])
        inp.append({"a": 2})  # 调用方改写原列表不得污染库内条目
        assert response_store.get("resp_b")["input_items"] == [{"a": 1}]

    def test_eviction_after_257(self):
        for i in range(257):
            response_store.save(f"resp_{i}", [i], [])
        assert response_store.size() == 256
        assert response_store.get("resp_0") is None  # 最旧被淘汰
        assert response_store.get("resp_1") is not None
        assert response_store.get("resp_256") is not None

    def test_get_promotes_lru_order(self):
        for i in range(256):
            response_store.save(f"resp_{i}", [i], [])
        response_store.get("resp_0")  # 命中移到队尾，脱离淘汰位次
        response_store.save("resp_new", ["new"], [])
        assert response_store.size() == 256
        assert response_store.get("resp_0") is not None      # 被提升，幸存
        assert response_store.get("resp_1") is None          # 成为最旧，被淘汰
        assert response_store.get("resp_new") is not None
