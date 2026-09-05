"""OpenAI Chat Completions 兼容层 —— /v1/chat/completions ↔ Anthropic Messages 双向转换。

入站：OpenAI 请求体 → Anthropic messages 体（system/developer 提取为 system 参数、
content 分块、tool_calls / tool_result / 图片(data URL) best-effort 映射）。
出站：Anthropic 响应（JSON 或 SSE 事件流）→ OpenAI 格式。

与 body_transform 同一原则：对畸形输入保持宽容，映射不了的部件安静跳过，
绝不放大请求失败。
"""

from __future__ import annotations

import json
import time
import uuid

# Anthropic stop_reason → OpenAI finish_reason
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _text_from_content(content: object) -> str:
    """OpenAI content（str | 分块数组）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return ""


def _image_block(url: str) -> dict | None:
    """data:image/...;base64,xxx → Anthropic image block；外链 URL 无法回填，跳过。"""
    if not url.startswith("data:"):
        return None
    head, _, b64 = url.partition(",")
    media_type = head[5:].split(";")[0] or "image/png"
    if not b64:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _blocks_from_user_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else [{"type": "text", "text": ""}]
    if not isinstance(content, list):
        return [{"type": "text", "text": ""}]
    blocks: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {})
            if isinstance(url, dict):
                url = url.get("url") or ""
            block = _image_block(str(url))
            if block:
                blocks.append(block)
    return blocks or [{"type": "text", "text": ""}]


def openai_to_anthropic(payload: dict) -> tuple[dict | None, str | None]:
    """OpenAI 请求体 → Anthropic messages 体。非法时返回 (None, 错误信息)。"""
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "必须提供 model 参数"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, "必须提供 messages 数组"

    system_parts: list[str] = []
    out_msgs: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _text_from_content(content)
            if text:
                system_parts.append(text)
        elif role == "tool":
            out_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(msg.get("tool_call_id") or ""),
                    "content": _text_from_content(content),
                }],
            })
        elif role == "assistant":
            blocks: list[dict] = []
            text = _text_from_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"_raw": args}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": args if isinstance(args, dict) else {},
                })
            out_msgs.append({
                "role": "assistant",
                "content": blocks or [{"type": "text", "text": ""}],
            })
        else:  # user 及未知角色一律按 user 处理
            out_msgs.append({"role": "user", "content": _blocks_from_user_content(content)})

    body: dict = {
        "model": model,
        "messages": out_msgs,
        "max_tokens": _as_int(payload.get("max_tokens") or payload.get("max_completion_tokens")) or 4096,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    try:
        if payload.get("temperature") is not None:
            body["temperature"] = float(payload["temperature"])
        if payload.get("top_p") is not None:
            body["top_p"] = float(payload["top_p"])
    except (TypeError, ValueError):
        pass
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        body["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        body["stop_sequences"] = [str(s) for s in stop]
    if payload.get("stream"):
        body["stream"] = True

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        mapped = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                mapped.append({
                    "name": str(fn["name"]),
                    "description": str(fn.get("description") or ""),
                    "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
                })
        if mapped:
            body["tools"] = mapped

    choice = payload.get("tool_choice")
    if choice == "none":
        body.pop("tools", None)
    elif choice == "required":
        body["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        name = str((choice.get("function") or {}).get("name") or "")
        if name:
            body["tool_choice"] = {"type": "tool", "name": name}
    # "auto"/缺省：Anthropic 默认即 auto，无需显式映射
    return body, None


def anthropic_to_openai(data: dict, model: str) -> dict:
    """Anthropic message 响应 → OpenAI chat.completion。"""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
    usage = data.get("usage") or {}
    in_tok = _as_int(usage.get("input_tokens")) or 0
    out_tok = _as_int(usage.get("output_tokens")) or 0
    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": str(data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(data.get("model") or model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _STOP_REASON_MAP.get(data.get("stop_reason"), "stop"),
        }],
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
    }


class StreamConverter:
    """Anthropic SSE 事件流 → OpenAI chat.completion.chunk 流（有状态转换器）。

    用法：先 start() 产出 role 首 chunk，逐条 feed(event_dict) 收输出行，
    流结束后追加 done()（"data: [DONE]"）。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.finish_reason: str | None = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._tool_seq = 0

    def start(self) -> str:
        return self._chunk({"role": "assistant", "content": ""})

    def done(self) -> str:
        return "data: [DONE]\n\n"

    def feed(self, evt: dict) -> list[str]:
        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message") or {}
            if msg.get("id"):
                self.chunk_id = str(msg["id"])
            u = msg.get("usage") or {}
            prompt = _as_int(u.get("input_tokens"))
            if prompt is not None:
                self.usage["prompt_tokens"] = prompt
                self.usage["total_tokens"] = prompt + self.usage["completion_tokens"]
            return []
        if etype == "content_block_start":
            block = evt.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = self._tool_seq
                self._tool_seq += 1
                return [self._chunk({"tool_calls": [{
                    "index": idx,
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""), "arguments": ""},
                }]})]
            return []
        if etype == "content_block_delta":
            delta = evt.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return [self._chunk({"content": delta["text"]})]
            if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                return [self._chunk({"tool_calls": [{
                    "index": max(self._tool_seq - 1, 0),
                    "function": {"arguments": delta["partial_json"]},
                }]})]
            return []
        if etype == "message_delta":
            delta = evt.get("delta") or {}
            self.finish_reason = _STOP_REASON_MAP.get(delta.get("stop_reason"), "stop")
            out = _as_int((evt.get("usage") or {}).get("output_tokens"))
            if out is not None:
                self.usage["completion_tokens"] = out
                self.usage["total_tokens"] = self.usage["prompt_tokens"] + out
            chunk = self._chunk({}, finish_reason=self.finish_reason)
            return [chunk]
        return []  # content_block_stop / message_stop / ping / error 等无需产出

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> str:
        payload: dict = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if finish_reason is not None:
            payload["usage"] = dict(self.usage)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
