"""OpenAI Responses API 兼容层 —— /v1/responses ↔ Chat Completions ↔ Anthropic。

官方 Responses 协议与 Chat Completions 的差异集中在三处：入站用 `input` 而不是
`messages`、出站是 `output[]` 条目数组、流式是 `response.*` 事件族。这里先把
Responses 请求翻译成 Chat Completions（复用既有的 chat→anthropic 与 anthropic→chat
双向层），再把 chat 结果/流翻译回 Responses 形态。

设计取舍（对齐 TriDefender 的 responses-handler）：
  - **无状态**：不实现 `previous_response_id`/`store` 的服务端会话（hub 本身无会话态）。
    带 `previous_response_id` 的请求直接 400，避免静默丢上下文。
  - 只做形状翻译，不做内容改写：映射不了的部件安静跳过（与 openai_compat 同原则）。
"""

from __future__ import annotations

import json
import time
import uuid

from .openai_compat import _as_int, _image_block

_MAX_OUTPUT_DEFAULT = 4096


def _text_from_responses_content(content: object) -> str:
    """Responses content（str | [{type:input_text|output_text|text,...}]）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "output_text", "text") and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(p for p in parts if p)


def _user_blocks(content: object) -> list[dict]:
    """Responses 用户内容 → anthropic 风格 content 分块（文本 + 图片）。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": ""}]
    blocks: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "output_text", "text") and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "input_image":
            url = item.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            block = _image_block(str(url or ""))
            if block:
                blocks.append(block)
    return blocks or [{"type": "text", "text": ""}]


def responses_to_chat(payload: dict) -> tuple[dict | None, str | None]:
    """Responses 请求体 → Chat Completions 请求体。非法时返回 (None, 错误)。"""
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "必须提供 model 参数"
    if payload.get("previous_response_id"):
        return None, "不支持 previous_response_id：本网关无服务端会话状态，请把历史放进 input"

    chat_messages: list[dict] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        chat_messages.append({"role": "system", "content": instructions})

    inp = payload.get("input")
    if isinstance(inp, str):
        chat_messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call":
                chat_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": item.get("arguments") if isinstance(item.get("arguments"), str)
                            else json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                        },
                    }],
                })
            elif itype == "function_call_output":
                chat_messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _text_from_responses_content(item.get("output")) or str(item.get("output") or ""),
                })
            elif itype in (None, "message"):
                role = str(item.get("role") or "user")
                content = item.get("content")
                if role in ("system", "developer"):
                    text = _text_from_responses_content(content)
                    if text:
                        chat_messages.append({"role": "system", "content": text})
                elif role == "assistant":
                    chat_messages.append({
                        "role": "assistant",
                        "content": _text_from_responses_content(content),
                    })
                else:
                    chat_messages.append({"role": "user", "content": _user_blocks(content)})
            # 其它条目（reasoning / item_reference 等）无对应语义，跳过
    elif inp is None:
        return None, "必须提供 input（string 或条目数组）"
    else:
        return None, "input 必须是字符串或数组"

    chat: dict = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": _as_int(payload.get("max_output_tokens")) or _MAX_OUTPUT_DEFAULT,
    }
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if payload.get(src) is not None:
            chat[dst] = payload[src]
    if payload.get("stream"):
        chat["stream"] = True

    tools = payload.get("tools")
    if isinstance(tools, list):
        mapped = []
        for t in tools:
            if not isinstance(t, dict) or t.get("type") != "function" or not t.get("name"):
                continue
            mapped.append({
                "type": "function",
                "function": {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "parameters": t.get("parameters") if isinstance(t.get("parameters"), dict) else {"type": "object"},
                },
            })
        if mapped:
            chat["tools"] = mapped

    choice = payload.get("tool_choice")
    if isinstance(choice, str):
        chat["tool_choice"] = choice
    elif isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        chat["tool_choice"] = {"type": "function", "function": {"name": str(choice["name"])}}

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        chat["reasoning_effort"] = reasoning["effort"]

    text_cfg = payload.get("text")
    if isinstance(text_cfg, dict):
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
            chat["response_format"] = fmt

    return chat, None


def _resp_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def chat_to_response(chat: dict, model: str) -> dict:
    """Chat Completions 响应 → Responses 响应对象。"""
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output: list[dict] = []
    output_text_parts: list[str] = []

    text = msg.get("content")
    if isinstance(text, str) and text:
        output_text_parts.append(text)
        output.append({
            "type": "message",
            "id": _item_id("msg"),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": _item_id("fc"),
            "call_id": str(tc.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or "{}"),
            "status": "completed",
        })

    usage = chat.get("usage") or {}
    in_tok = _as_int(usage.get("prompt_tokens")) or 0
    out_tok = _as_int(usage.get("completion_tokens")) or 0
    finish = choice.get("finish_reason")
    return {
        "id": _resp_id(),
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if finish == "length" else "completed",
        "model": str(chat.get("model") or model),
        "output": output,
        "output_text": "".join(output_text_parts),
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if finish == "length" else None,
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


class ResponsesStreamConverter:
    """Chat chunk 流 → Responses SSE 事件族。

    输入是 `StreamConverter.feed_events()` 产出的 chunk dict；输出是
    `event: <type>\\ndata: <json>\\n\\n` 行（Responses 协议要求带 event 名）。

    不变量（对齐 OpenAI Responses SSE）：
      - `sequence_number` 从 **0** 起单调递增；
      - `output_index` 按 item **创建顺序**分配（message 与 function_call 一视
        同仁）——added/delta/done 三处同值，且与最终 `response.output` 数组
        顺序一致。不能对 message 写死 0、对 tool 写死 idx+1：上游块序若为
        tool→text，那样会产出乱序编号。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.response_id = _resp_id()
        self.created_at = int(time.time())
        self.message_item_id = _item_id("msg")
        self.message_open = False
        self.text_parts: list[str] = []
        self.tool_items: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: dict = {"prompt_tokens": None, "completion_tokens": None}
        self._sequence = -1  # 首个 _event() 自增后为 0
        self._next_output = 0
        self.message_output_index: int | None = None
        self.tool_output_index: dict[int, int] = {}

    def _event(self, etype: str, payload: dict) -> str:
        self._sequence += 1
        payload = {"type": etype, "sequence_number": self._sequence, **payload}
        return f"event: {etype}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _response_skeleton(self, status: str, output: list[dict]) -> dict:
        in_tok = _as_int(self.usage.get("prompt_tokens")) or 0
        out_tok = _as_int(self.usage.get("completion_tokens")) or 0
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "output_text": "".join(self.text_parts),
            "usage": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            "error": None,
            "incomplete_details": None,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

    def fail(self, message: str, code: str | None = None) -> list[str]:
        """流中途失败：产出 response.failed 终态事件（对齐 Responses 协议）。"""
        final = self._response_skeleton("failed", [])
        final["error"] = {"code": code or "upstream_error", "message": message}
        return [self._event("response.failed", {"response": final})]

    def start(self) -> list[str]:
        return [
            self._event("response.created", {"response": self._response_skeleton("in_progress", [])}),
            self._event("response.in_progress", {"response": self._response_skeleton("in_progress", [])}),
        ]

    def _open_message(self) -> list[str]:
        if self.message_open:
            return []
        self.message_open = True
        self.message_output_index = self._next_output
        self._next_output += 1
        item = {
            "type": "message",
            "id": self.message_item_id,
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        return [
            self._event("response.output_item.added", {
                "output_index": self.message_output_index, "item": item}),
            self._event("response.content_part.added", {
                "item_id": self.message_item_id,
                "output_index": self.message_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ]

    def feed_chunk(self, chunk: dict) -> list[str]:
        out: list[str] = []
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        usage = chunk.get("usage") or {}
        if usage:
            self.usage["prompt_tokens"] = usage.get("prompt_tokens")
            self.usage["completion_tokens"] = usage.get("completion_tokens")

        text = delta.get("content")
        if isinstance(text, str) and text:
            out.extend(self._open_message())
            self.text_parts.append(text)
            out.append(self._event("response.output_text.delta", {
                "item_id": self.message_item_id,
                "output_index": self.message_output_index,
                "content_index": 0,
                "delta": text,
            }))
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = _as_int(tc.get("index")) or 0
            fn = tc.get("function") or {}
            if idx not in self.tool_items:
                item = {
                    "type": "function_call",
                    "id": _item_id("fc"),
                    "call_id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": "",
                    "status": "in_progress",
                }
                self.tool_items[idx] = item
                self.tool_output_index[idx] = self._next_output
                self._next_output += 1
                out.append(self._event("response.output_item.added", {
                    "output_index": self.tool_output_index[idx], "item": dict(item),
                }))
            args = fn.get("arguments")
            if isinstance(args, str) and args:
                self.tool_items[idx]["arguments"] += args
                out.append(self._event("response.function_call_arguments.delta", {
                    "item_id": self.tool_items[idx]["id"],
                    "output_index": self.tool_output_index[idx],
                    "delta": args,
                }))
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        return out

    def done(self) -> list[str]:
        out: list[str] = []
        text = "".join(self.text_parts)
        final_items: list[tuple[int, dict]] = []
        if self.message_open:
            out.append(self._event("response.output_text.done", {
                "item_id": self.message_item_id,
                "output_index": self.message_output_index,
                "content_index": 0,
                "text": text,
            }))
            out.append(self._event("response.content_part.done", {
                "item_id": self.message_item_id,
                "output_index": self.message_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }))
            message_item = {
                "type": "message",
                "id": self.message_item_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            out.append(self._event("response.output_item.done", {
                "output_index": self.message_output_index, "item": message_item,
            }))
            final_items.append((self.message_output_index, message_item))
        for idx, item in sorted(self.tool_items.items()):
            out.append(self._event("response.function_call_arguments.done", {
                "item_id": item["id"],
                "output_index": self.tool_output_index[idx],
                "arguments": item["arguments"],
            }))
            item["status"] = "completed"
            final_items.append((self.tool_output_index[idx], dict(item)))
            out.append(self._event("response.output_item.done", {
                "output_index": self.tool_output_index[idx], "item": dict(item),
            }))

        # 最终 output 数组按 output_index 排序（= item 创建顺序）
        final_items.sort(key=lambda pair: pair[0])
        output_items = [item for _, item in final_items]
        status = "incomplete" if self.finish_reason == "length" else "completed"
        final = self._response_skeleton(status, output_items)
        if status == "incomplete":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        out.append(self._event("response.completed", {"response": final}))
        return out
