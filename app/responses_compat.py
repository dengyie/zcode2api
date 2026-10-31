"""OpenAI Responses 兼容层 —— /v1/responses ↔ Anthropic Messages 双向转换。

入站：Responses 请求体 → Anthropic messages 体（instructions→system、input
条目（message/reasoning/function_call/function_call_output）→ blocks、
reasoning.effort→reasoning_effort、tools 扁平映射）。
出站：Anthropic 响应（JSON 或 SSE 事件流）→ Responses 格式
（output 条目 + response.created/.completed 事件帧）。

与 openai_compat 同一原则：对畸形输入保持宽容，映射不了的条目安静跳过，
绝不放大请求失败。previous_response_id 的历史合并由网关层（response_store）
完成后并入 input，本模块只做纯格式翻译。
"""

from __future__ import annotations

import json
import time
import uuid

# Responses reasoning.effort → Anthropic reasoning_effort
# 上游 GLM 原生接受 reasoning_effort（low/high/max），不支持关闭思考，
# medium 即上游默认档——不映射、不带该字段。
_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "high": "high",
    "xhigh": "max",
}


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _image_block(url: str) -> dict | None:
    """data:image/...;base64,xxx → Anthropic image block；外链 URL 无法回填，跳过。"""
    if not url.startswith("data:"):
        return None
    head, _, b64 = url.partition(",")
    media_type = head[5:].split(";")[0] or "image/png"
    if not b64:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _message_to_blocks(item: dict) -> dict | None:
    """message 条目 → Anthropic 消息；无有效内容返回 None（整条丢弃）。

    兼容两种入参形态：
    - Responses 规范：type="message"，分块用 input_text / output_text
    - chat 简写（部分客户端的 input 数组就长这样）：无 type，分块用 text——
      缺 type 的条目若带 role 同样按消息处理，否则会被当未知条目丢弃，
      最终发出空 messages 招致上游 400（实测 code 1214）。
    assistant 取 output_text/text，user（及其他角色按 user）取
    input_text/text 与 input_image（仅 data URL）。
    """
    role = item.get("role")
    content = item.get("content")
    if role == "assistant":
        blocks: list[dict] = []
        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if (isinstance(part, dict) and part.get("type") in ("output_text", "text")
                        and isinstance(part.get("text"), str) and part["text"]):
                    blocks.append({"type": "text", "text": part["text"]})
        return {"role": "assistant", "content": blocks} if blocks else None
    # user 及未知角色一律按 user 处理
    blocks = []
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("input_text", "text") and isinstance(part.get("text"), str) and part["text"]:
                blocks.append({"type": "text", "text": part["text"]})
            elif ptype == "input_image":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url") or ""
                block = _image_block(str(url))
                if block:
                    blocks.append(block)
    return {"role": "user", "content": blocks} if blocks else None


def _reasoning_to_thinking(item: dict) -> dict | None:
    """type=="reasoning" 条目 → assistant 消息（thinking 块）。

    优先取 content[]（type reasoning_text）的原文，缺失退回 summary[]（type
    summary_text）；两者皆空则整条跳过。thinking 块 signature 上游不校验
    （实测伪造值可通过），空串亦可——同 openai_compat 重放 reasoning 的先例。
    """
    texts: list[str] = []
    for part in item.get("content") or []:
        if (isinstance(part, dict) and part.get("type") == "reasoning_text"
                and isinstance(part.get("text"), str) and part["text"]):
            texts.append(part["text"])
    if not texts:
        for part in item.get("summary") or []:
            if (isinstance(part, dict) and part.get("type") == "summary_text"
                    and isinstance(part.get("text"), str) and part["text"]):
                texts.append(part["text"])
    text = "\n".join(texts)
    if not text:
        return None
    return {"role": "assistant", "content": [{"type": "thinking", "thinking": text, "signature": ""}]}


def _function_call_to_msg(item: dict) -> dict:
    """type=="function_call" 条目 → assistant 消息（tool_use 块）。

    arguments 为 JSON 字符串，解析失败不丢数据，包成 {"_raw": ...} 上送
    （同 openai_compat 的 tool_calls 宽容策略）。
    """
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            args = {"_raw": args}
    elif not isinstance(args, dict):
        args = {}
    return {"role": "assistant", "content": [{
        "type": "tool_use",
        "id": str(item.get("call_id") or ""),
        "name": str(item.get("name") or ""),
        "input": args if isinstance(args, dict) else {},
    }]}


def _function_output_to_msg(item: dict) -> dict:
    """type=="function_call_output" 条目 → user 消息（tool_result 块）。

    output 为 str 直接用；为 list 时拼接其中 output_text 的 text
    （ Responses 规范的 content parts 形态）。
    """
    output = item.get("output")
    if isinstance(output, list):
        parts = []
        for part in output:
            if (isinstance(part, dict) and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)):
                parts.append(part["text"])
        content = "\n".join(p for p in parts if p)
    elif isinstance(output, str):
        content = output
    else:
        content = ""
    return {"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": str(item.get("call_id") or ""),
        "content": content,
    }]}


def responses_to_anthropic(payload: dict) -> tuple[dict | None, str | None]:
    """Responses 请求体 → Anthropic messages 体。非法时返回 (None, 错误信息)。"""
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "必须提供 model 参数"

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        # 字符串 input → 单条 user 消息
        items: list = []
        if raw_input:
            items = [{"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": raw_input}]}]
    elif isinstance(raw_input, list):
        items = raw_input
    else:
        items = []
    # input 为空且无 previous_response_id 链入内容时才报错
    #（网关层命中历史后已合并进 input；直接调用时以 previous_response_id 字段为准）
    if not items and not payload.get("previous_response_id"):
        return None, "必须提供 input"

    out_msgs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue  # 非 dict 条目安静跳过
        itype = item.get("type")
        # type 缺失但带 role 的条目按消息处理（客户端 chat 简写发法）
        if itype == "message" or (itype is None and item.get("role")):
            msg = _message_to_blocks(item)
            if msg is not None:
                out_msgs.append(msg)
        elif itype == "reasoning":
            msg = _reasoning_to_thinking(item)
            if msg is not None:
                out_msgs.append(msg)
        elif itype == "function_call":
            out_msgs.append(_function_call_to_msg(item))
        elif itype == "function_call_output":
            out_msgs.append(_function_output_to_msg(item))
        # item_reference / 未知类型：安静跳过

    # 相邻同 role 消息合并（blocks 拼接；Anthropic 要求 role 严格交替）
    messages: list[dict] = []
    for msg in out_msgs:
        if messages and messages[-1]["role"] == msg["role"]:
            messages[-1]["content"] = messages[-1]["content"] + msg["content"]
        else:
            messages.append(msg)
    # 全部条目都映射不出内容 → 本地 400 早失败：空 messages 发上游只会换来
    # 语焉不详的 1214「messages 参数非法」，客户端无法据此定位入参问题
    if not messages:
        return None, ("input 未包含可用消息条目（支持 message/reasoning/"
                      "function_call/function_call_output，message 条目可省略 type 只留 role+content）")

    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": _as_int(payload.get("max_output_tokens")) or 4096,
    }
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        body["system"] = instructions
    elif isinstance(instructions, list):
        parts = [p for p in instructions if isinstance(p, str) and p.strip()]
        if parts:
            body["system"] = "\n\n".join(parts)
    try:
        if payload.get("temperature") is not None:
            body["temperature"] = float(payload["temperature"])
        if payload.get("top_p") is not None:
            body["top_p"] = float(payload["top_p"])
    except (TypeError, ValueError):
        pass
    # 推理强度：上游 GLM 原生接受 reasoning_effort（low/high/max），不支持关思考
    # —— minimal/low 降到 low（低速档），xhigh 升到 max，medium 即默认档不带字段
    effort = (payload.get("reasoning") or {}).get("effort") if isinstance(payload.get("reasoning"), dict) else None
    if isinstance(effort, str) and effort.strip().lower() in _EFFORT_MAP:
        body["reasoning_effort"] = _EFFORT_MAP[effort.strip().lower()]
    if payload.get("stream"):
        body["stream"] = True

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        mapped = []
        for t in tools:
            # 仅 function 工具可映射；web_search 等内置类型上游无对应物，跳过
            if isinstance(t, dict) and t.get("type") == "function" and t.get("name"):
                mapped.append({
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "input_schema": t.get("parameters") if isinstance(t.get("parameters"), dict) else {"type": "object"},
                })
        if mapped:
            body["tools"] = mapped

    choice = payload.get("tool_choice")
    if choice == "none":
        body.pop("tools", None)
    elif choice == "required":
        body["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        name = str(choice.get("name") or "")
        if name:
            body["tool_choice"] = {"type": "tool", "name": name}
    # "auto"/缺省：Anthropic 默认即 auto，无需显式映射
    return body, None


def anthropic_to_responses(data: dict, model: str) -> dict:
    """Anthropic message 响应 → OpenAI response 对象。"""
    output: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking" and isinstance(block.get("thinking"), str):
            output.append({
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex[:12]}",
                "summary": [{"type": "summary_text", "text": block["thinking"]}],
            })
        elif btype == "text" and isinstance(block.get("text"), str):
            output.append({
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": block["text"], "annotations": []}],
            })
        elif btype == "tool_use":
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:12]}",
                "call_id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
            })
    usage = data.get("usage") or {}
    in_tok = _as_int(usage.get("input_tokens")) or 0
    out_tok = _as_int(usage.get("output_tokens")) or 0
    if data.get("stop_reason") == "max_tokens":
        status = "incomplete"
        incomplete_details: dict | None = {"reason": "max_output_tokens"}
    else:
        status = "completed"
        incomplete_details = None
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": str(data.get("model") or model),
        "output": output,
        "error": None,
        "incomplete_details": incomplete_details,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "input_tokens_details": {
                "cached_tokens": _as_int(usage.get("cache_read_input_tokens")) or 0,
            },
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


class ResponsesStreamConverter:
    """Anthropic SSE 事件流 → OpenAI Responses 事件流（有状态转换器）。

    用法：先 start() 产出 response.created + response.in_progress 两帧，
    逐条 feed(event_dict) 收输出帧，流结束后调用 done()。每帧形如
    `event: <type>\\ndata: {json}\\n\\n`，data 内 sequence_number 严格递增。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.response_id = f"resp_{uuid.uuid4().hex[:24]}"
        self.created_at = int(time.time())
        self.sequence_number = 0
        # None = 上游未上报（未知），0 = 真实为零；两者语义不同
        self.usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        self.output_items: list[dict] = []  # 已完成的 output 条目（response.completed 引用）
        self._stop_reason: str | None = None
        self._output_index = -1   # 当前 content block 的 output_index
        self._cur_type: str | None = None
        self._cur_item_id: str | None = None
        self._cur_text = ""       # thinking / text 累积
        self._cur_args = ""       # tool_use arguments 累积
        self._cur_call_id = ""
        self._cur_name = ""

    def _sync_usage_total(self) -> None:
        i, o = self.usage["input_tokens"], self.usage["output_tokens"]
        self.usage["total_tokens"] = (i or 0) + (o or 0) if i is not None or o is not None else None

    def _reset_cur(self) -> None:
        self._cur_type = None
        self._cur_item_id = None
        self._cur_text = ""
        self._cur_args = ""
        self._cur_call_id = ""
        self._cur_name = ""

    # ── 帧构造 ────────────────────────────────────────────────────────────
    def _event(self, etype: str, payload: dict) -> str:
        frame = {"type": etype, "sequence_number": self.sequence_number, **payload}
        self.sequence_number += 1
        return f"event: {etype}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n"

    def _snapshot(self, status: str, output: list) -> dict:
        return {
            "id": self.response_id,
            "object": "response",
            "type": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "error": None,
            "incomplete_details": None,
            "usage": dict(self.usage),
        }

    def start(self) -> str:
        snap = self._snapshot("in_progress", [])
        return self._event("response.created", {"response": snap}) \
            + self._event("response.in_progress", {"response": snap})

    def done(self) -> str:
        # Responses 流没有 [DONE] 哨兵（OpenAI 规范以 response.completed 收尾），
        # 恒返回空串，网关据此不再 yield 任何收尾帧
        return ""

    # ── 上游事件处理 ──────────────────────────────────────────────────────
    def feed(self, evt: dict) -> list[str]:
        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message") or {}
            if msg.get("id"):
                self.response_id = f"resp_{msg['id']}"
            in_tok = _as_int((msg.get("usage") or {}).get("input_tokens"))
            if in_tok is not None:
                self.usage["input_tokens"] = in_tok
                self._sync_usage_total()
            return []
        if etype == "content_block_start":
            return self._on_block_start(evt.get("content_block") or {})
        if etype == "content_block_delta":
            return self._on_block_delta(evt.get("delta") or {})
        if etype == "content_block_stop":
            return self._on_block_stop()
        if etype == "message_delta":
            delta = evt.get("delta") or {}
            if delta.get("stop_reason"):
                self._stop_reason = str(delta["stop_reason"])
            out = _as_int((evt.get("usage") or {}).get("output_tokens"))
            if out is not None:
                self.usage["output_tokens"] = out
                self._sync_usage_total()
            return []
        if etype == "message_stop":
            return [self._completed_frame()]
        return []  # ping / error 等无需产出

    def _on_block_start(self, block: dict) -> list[str]:
        btype = block.get("type")
        if btype not in ("thinking", "text", "tool_use"):
            return []  # 未识别的块不占 output_index，安静跳过
        self._output_index += 1
        self._cur_type = btype
        idx = self._output_index
        if btype == "thinking":
            self._cur_item_id = f"rs_{uuid.uuid4().hex[:12]}"
            return [
                self._event("response.output_item.added", {
                    "output_index": idx,
                    "item": {"type": "reasoning", "id": self._cur_item_id, "summary": []},
                }),
                self._event("response.reasoning_summary_part.added", {
                    "item_id": self._cur_item_id, "output_index": idx, "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }),
            ]
        if btype == "text":
            self._cur_item_id = f"msg_{uuid.uuid4().hex[:12]}"
            return [
                self._event("response.output_item.added", {
                    "output_index": idx,
                    "item": {"type": "message", "id": self._cur_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }),
                self._event("response.content_part.added", {
                    "item_id": self._cur_item_id, "output_index": idx, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }),
            ]
        # tool_use
        self._cur_item_id = f"fc_{uuid.uuid4().hex[:12]}"
        self._cur_call_id = str(block.get("id") or "")
        self._cur_name = str(block.get("name") or "")
        return [self._event("response.output_item.added", {
            "output_index": idx,
            "item": {"type": "function_call", "id": self._cur_item_id,
                     "call_id": self._cur_call_id, "name": self._cur_name,
                     "arguments": "", "status": "in_progress"},
        })]

    def _on_block_delta(self, delta: dict) -> list[str]:
        dtype = delta.get("type")
        idx = self._output_index
        item_id = self._cur_item_id
        if dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
            self._cur_text += delta["thinking"]
            return [self._event("response.reasoning_summary_text.delta", {
                "item_id": item_id, "output_index": idx, "content_index": 0,
                "delta": delta["thinking"],
            })]
        if dtype == "text_delta" and isinstance(delta.get("text"), str):
            self._cur_text += delta["text"]
            return [self._event("response.output_text.delta", {
                "item_id": item_id, "output_index": idx, "content_index": 0,
                "delta": delta["text"],
            })]
        if dtype == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            self._cur_args += delta["partial_json"]
            return [self._event("response.function_call_arguments.delta", {
                "item_id": item_id, "output_index": idx, "content_index": 0,
                "delta": delta["partial_json"],
            })]
        return []

    def _on_block_stop(self) -> list[str]:
        idx = self._output_index
        item_id = self._cur_item_id
        if self._cur_type == "thinking":
            item = {"type": "reasoning", "id": item_id,
                    "summary": [{"type": "summary_text", "text": self._cur_text}]}
            frames = [
                self._event("response.reasoning_summary_text.done", {
                    "item_id": item_id, "output_index": idx, "content_index": 0,
                    "text": self._cur_text,
                }),
                self._event("response.reasoning_summary_part.done", {
                    "item_id": item_id, "output_index": idx, "summary_index": 0,
                    "part": {"type": "summary_text", "text": self._cur_text},
                }),
            ]
        elif self._cur_type == "text":
            item = {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": self._cur_text, "annotations": []}]}
            frames = [
                self._event("response.output_text.done", {
                    "item_id": item_id, "output_index": idx, "content_index": 0,
                    "text": self._cur_text,
                }),
                self._event("response.content_part.done", {
                    "item_id": item_id, "output_index": idx, "content_index": 0,
                    "part": {"type": "output_text", "text": self._cur_text, "annotations": []},
                }),
            ]
        elif self._cur_type == "tool_use":
            item = {"type": "function_call", "id": item_id, "call_id": self._cur_call_id,
                    "name": self._cur_name, "arguments": self._cur_args, "status": "completed"}
            frames = [
                self._event("response.function_call_arguments.done", {
                    "item_id": item_id, "output_index": idx,
                    "arguments": self._cur_args,
                }),
            ]
        else:
            return []
        frames.append(self._event("response.output_item.done", {
            "output_index": idx, "item": item,
        }))
        self.output_items.append(item)
        self._reset_cur()
        return frames

    def _completed_frame(self) -> str:
        if self._stop_reason == "max_tokens":
            status = "incomplete"
            details: dict | None = {"reason": "max_output_tokens"}
        else:
            status = "completed"
            details = None
        snap = self._snapshot(status, list(self.output_items))
        snap["incomplete_details"] = details
        return self._event("response.completed", {"response": snap})
