"""token 估算 —— `/v1/messages/count_tokens` 的本地实现。

上游（zcode-plan anthropic 网关与 api.z.ai）**没有** count_tokens 端点
（2026-11-02 实测 404），但 Anthropic SDK 客户端会先调它做预算。这里给一个
确定性估算器：不追求与 GLM 分词器逐 token 对齐，目标是「量级正确、单调、
不把客户端吓退」。

口径：
  - ASCII/拉丁字符按 4 字符 ≈ 1 token；
  - CJK 等非拉丁字符按 1 字符 ≈ 1 token；
  - 每条消息 +4（role/分隔符开销），图片 +1600（anthropic 的图片计价口径）；
  - tools 的 JSON Schema 按文本估算。
"""

from __future__ import annotations

import json
import math


def _text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = 0
    wide_chars = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            wide_chars += 1
    return math.ceil(ascii_chars / 4) + wide_chars


def _content_tokens(content: object) -> int:
    if isinstance(content, str):
        return _text_tokens(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            total += _text_tokens(block["text"])
        elif btype == "image":
            total += 1600
        elif btype == "tool_use":
            total += _text_tokens(str(block.get("name") or ""))
            total += _text_tokens(json.dumps(block.get("input") or {}, ensure_ascii=False))
        elif btype == "tool_result":
            total += _content_tokens(block.get("content"))
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            total += _text_tokens(block["thinking"])
    return total


def estimate_input_tokens(body: dict) -> int:
    """估算 anthropic messages 请求体的 input tokens（含 system / messages / tools）。"""
    total = 0
    system = body.get("system")
    if isinstance(system, str):
        total += _text_tokens(system)
    elif isinstance(system, list):
        total += _content_tokens(system)
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            total += 4
            total += _content_tokens(msg.get("content"))
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                total += _text_tokens(json.dumps(tool, ensure_ascii=False))
    return max(total, 1)
