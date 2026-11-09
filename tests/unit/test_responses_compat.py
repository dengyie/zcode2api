"""responses_compat + token_estimate 单元测试 —— /v1/responses 形状翻译与 token 估算。"""

from __future__ import annotations

import json

from app.responses_compat import ResponsesStreamConverter, chat_to_response, responses_to_chat
from app.token_estimate import estimate_input_tokens


class TestResponsesToChat:
    def test_string_input(self):
        chat, err = responses_to_chat({"model": "glm-5.3", "input": "你好"})
        assert err is None
        assert chat["messages"] == [{"role": "user", "content": "你好"}]
        assert chat["max_tokens"] == 4096

    def test_instructions_become_system(self):
        chat, err = responses_to_chat({
            "model": "glm-5.3",
            "instructions": "只答要点",
            "input": "hi",
        })
        assert err is None
        assert chat["messages"][0] == {"role": "system", "content": "只答要点"}

    def test_message_items_with_input_text(self):
        chat, err = responses_to_chat({
            "model": "glm-5.3",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "第一问"}, {"type": "input_text", "text": "第二问"}]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "回答"}]},
            ],
        })
        assert err is None
        assert chat["messages"][0]["content"] == [{"type": "text", "text": "第一问"}, {"type": "text", "text": "第二问"}]
        assert chat["messages"][1] == {"role": "assistant", "content": "回答"}

    def test_function_call_roundtrip(self):
        chat, err = responses_to_chat({
            "model": "glm-5.3",
            "input": [
                {"type": "message", "role": "user", "content": "天气?"},
                {"type": "function_call", "call_id": "call_1", "name": "get_weather",
                 "arguments": '{"city":"北京"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "晴"},
            ],
        })
        assert err is None
        assert chat["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert json.loads(chat["messages"][1]["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
        assert chat["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "晴"}

    def test_previous_response_id_rejected(self):
        chat, err = responses_to_chat({"model": "x", "input": "hi", "previous_response_id": "resp_1"})
        assert chat is None
        assert "previous_response_id" in err

    def test_missing_model_and_input(self):
        assert responses_to_chat({"input": "hi"})[0] is None
        assert responses_to_chat({"model": "x"})[0] is None
        assert responses_to_chat({"model": "x", "input": 42})[0] is None

    def test_tools_and_effort_and_json_format(self):
        chat, err = responses_to_chat({
            "model": "glm-5.3",
            "input": "hi",
            "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}},
                      {"type": "web_search"}],
            "tool_choice": "auto",
            "reasoning": {"effort": "high"},
            "text": {"format": {"type": "json_object"}},
            "max_output_tokens": 128,
        })
        assert err is None
        assert len(chat["tools"]) == 1  # 非 function 工具跳过
        assert chat["tools"][0]["function"]["name"] == "f"
        assert chat["reasoning_effort"] == "high"
        assert chat["response_format"] == {"type": "json_object"}
        assert chat["max_tokens"] == 128

    def test_parallel_tool_calls_and_stream_passthrough(self):
        chat, err = responses_to_chat({"model": "x", "input": "hi", "stream": True, "temperature": 0.2})
        assert err is None
        assert chat["stream"] is True
        assert chat["temperature"] == 0.2


class TestChatToResponse:
    def test_text_only(self):
        resp = chat_to_response({
            "model": "glm-5.3",
            "choices": [{"message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }, "glm-5.3")
        assert resp["object"] == "response"
        assert resp["status"] == "completed"
        assert resp["output_text"] == "你好"
        assert resp["output"][0]["content"][0] == {"type": "output_text", "text": "你好", "annotations": []}
        assert resp["usage"]["input_tokens"] == 10
        assert resp["usage"]["total_tokens"] == 13

    def test_tool_calls_become_function_call_items(self):
        resp = chat_to_response({
            "choices": [{
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "f", "arguments": '{"a":1}'},
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }, "m")
        assert resp["output"][0]["type"] == "function_call"
        assert resp["output"][0]["call_id"] == "call_1"
        assert resp["output"][0]["arguments"] == '{"a":1}'

    def test_length_finish_marks_incomplete(self):
        resp = chat_to_response({
            "choices": [{"message": {"content": "截断"}, "finish_reason": "length"}],
            "usage": {},
        }, "m")
        assert resp["status"] == "incomplete"
        assert resp["incomplete_details"] == {"reason": "max_output_tokens"}


def _lines(events: list[str]) -> list[tuple[str, dict]]:
    out = []
    for ev in events:
        etype = data = None
        for line in ev.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        out.append((etype, data))
    return out


class TestResponsesStreamConverter:
    def test_text_stream_event_order(self):
        conv = ResponsesStreamConverter("glm-5.3")
        events = conv.start()
        events += conv.feed_chunk({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"content": "你"}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"content": "好"}}]})
        events += conv.feed_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
        events += conv.done()
        pairs = _lines(events)
        types = [t for t, _ in pairs]

        assert types[0] == "response.created"
        assert types[1] == "response.in_progress"
        assert types[2] == "response.output_item.added"
        assert types[3] == "response.content_part.added"
        assert types.count("response.output_text.delta") == 2
        assert types[-1] == "response.completed"
        assert types[-2] == "response.output_item.done"

        # sequence_number 从 0 起严格连续递增（对齐 OpenAI Responses SSE）
        seqs = [d["sequence_number"] for _, d in pairs]
        assert seqs == list(range(len(seqs)))

        final = pairs[-1][1]["response"]
        assert final["output_text"] == "你好"
        assert final["usage"]["input_tokens"] == 7
        assert final["usage"]["output_tokens"] == 2
        assert final["status"] == "completed"

    def test_tool_call_stream(self):
        conv = ResponsesStreamConverter("m")
        events = conv.start()
        events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "f", "arguments": ""},
        }]}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '{"a":'}}]}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": "1}"}}]}}]})
        events += conv.done()
        pairs = _lines(events)
        types = [t for t, _ in pairs]
        assert "response.function_call_arguments.delta" in types
        done_ev = [d for t, d in pairs if t == "response.function_call_arguments.done"][0]
        assert done_ev["arguments"] == '{"a":1}'
        final = pairs[-1][1]["response"]
        assert final["output"][0]["type"] == "function_call"
        assert final["output"][0]["arguments"] == '{"a":1}'

    def test_empty_stream_still_completes(self):
        conv = ResponsesStreamConverter("m")
        events = conv.start() + conv.done()
        final = _lines(events)[-1][1]["response"]
        assert final["status"] == "completed"
        assert final["output"] == []


class TestEstimateInputTokens:
    def test_basic_shape(self):
        n = estimate_input_tokens({"messages": [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]})
        assert n >= 1

    def test_cjk_costs_more_than_ascii(self):
        ascii_n = estimate_input_tokens({"messages": [{"role": "user", "content": "a" * 40}]})
        cjk_n = estimate_input_tokens({"messages": [{"role": "user", "content": "中" * 40}]})
        assert cjk_n > ascii_n

    def test_monotonic_and_counts_system_and_tools(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        with_system = {**base, "system": "规则" * 20}
        with_tools = {**base, "tools": [{"name": "f", "description": "d" * 80, "input_schema": {"type": "object"}}]}
        assert estimate_input_tokens(with_system) > estimate_input_tokens(base)
        assert estimate_input_tokens(with_tools) > estimate_input_tokens(base)

    def test_image_and_empty(self):
        with_img = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
        ]}]}
        assert estimate_input_tokens(with_img) >= 1600
        assert estimate_input_tokens({}) >= 1

    def test_string_system_and_tool_result(self):
        n = estimate_input_tokens({
            "system": "sys",
            "messages": [
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                             "content": [{"type": "text", "text": "结果"}]}]},
            ],
        })
        assert n >= 1


class TestStreamInvariants:
    """序列号与 output_index 的协议不变量（2026-11-03 review 修复项）。"""

    def test_text_first_output_indices(self):
        conv = ResponsesStreamConverter("m")
        events = conv.start()
        events += conv.feed_chunk({"choices": [{"delta": {"content": "你"}}]})
        events += conv.feed_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        events += conv.done()
        pairs = _lines(events)
        added = [d["output_index"] for t, d in pairs if t == "response.output_item.added"]
        assert added == [0]
        deltas = [d["output_index"] for t, d in pairs if t == "response.output_text.delta"]
        assert deltas == [0]
        done_idx = [d["output_index"] for t, d in pairs if t == "response.output_item.done"]
        assert done_idx == [0]

    def test_tool_first_creation_order_indices(self):
        """tool 先于 text 出现时，编号按 item 创建顺序：tool=0、message=1。

        旧实现对 message 写死 0、tool 写死 idx+1，tool-first 流会产出乱序编号
        且 added/delta/done 三处不一致。
        """
        conv = ResponsesStreamConverter("m")
        events = conv.start()
        events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "f", "arguments": ""}},
        ]}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"a":1}'}},
        ]}}]})
        events += conv.feed_chunk({"choices": [{"delta": {"content": "hi"}}]})
        events += conv.done()
        pairs = _lines(events)
        added = [d["output_index"] for t, d in pairs if t == "response.output_item.added"]
        assert added == [0, 1]  # added 事件严格递增（创建顺序）
        args_delta = [d for t, d in pairs if t == "response.function_call_arguments.delta"][0]
        assert args_delta["output_index"] == 0  # 与该工具 added 时一致
        done_by_type = {d["item"]["type"]: d["output_index"]
                        for t, d in pairs if t == "response.output_item.done"}
        assert done_by_type == {"function_call": 0, "message": 1}
        final = pairs[-1][1]["response"]
        assert [i["type"] for i in final["output"]] == ["function_call", "message"]  # output 数组同序

    def test_two_tools_indices(self):
        conv = ResponsesStreamConverter("m")
        events = conv.start()
        for i, name in enumerate(("f1", "f2")):
            events += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [
                {"index": i, "id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}},
            ]}}]})
        events += conv.done()
        pairs = _lines(events)
        added = [d["output_index"] for t, d in pairs if t == "response.output_item.added"]
        assert added == [0, 1]
        final = pairs[-1][1]["response"]
        assert [i["call_id"] for i in final["output"]] == ["c0", "c1"]

    def test_no_payload_param(self):
        """构造器不再接收 request_payload（review Nit：存而不用）。"""
        import inspect
        assert list(inspect.signature(ResponsesStreamConverter.__init__).parameters) == ["self", "model"]


class TestEstimateAfterTransform:
    """count_tokens 必须估算 transform 后形状（2026-11-03 线上标定：固定开销 ≈400 tok）。

    实测：6 tok 的原始请求上游计费 341——只算原始 body 对小请求低估近百倍。
    """

    def test_includes_transform_overhead(self):
        from app.token_estimate import estimate_input_tokens, estimate_input_tokens_after_transform

        body = {"model": "GLM-5.3-Flash",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}
        raw = estimate_input_tokens(body)
        after = estimate_input_tokens_after_transform(body, "GLM-5.3-Flash")
        assert after - raw >= 300  # 官方三块 + 日期轮 ≈ 400 tok
        assert after >= 300

    def test_deterministic(self):
        from app.token_estimate import estimate_input_tokens_after_transform

        body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}
        assert estimate_input_tokens_after_transform(body, "GLM-5.3") \
            == estimate_input_tokens_after_transform(body, "GLM-5.3")

    def test_scales_with_content(self):
        from app.token_estimate import estimate_input_tokens_after_transform

        small = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        big = {"model": "m", "messages": [{"role": "user", "content": "长" * 500}]}
        assert estimate_input_tokens_after_transform(big, "m") \
            > estimate_input_tokens_after_transform(small, "m")

    def test_original_body_not_mutated(self):
        from app.token_estimate import estimate_input_tokens_after_transform

        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        estimate_input_tokens_after_transform(body, "m")
        assert "system" not in body  # transform 在副本上做，调用方 body 不被污染
