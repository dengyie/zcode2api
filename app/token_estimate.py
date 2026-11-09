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

import copy
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


def estimate_input_tokens_after_transform(body: dict, model: str) -> int:
    """估算 **transform 之后**（上游实际计费形状）的 input tokens。

    Plan 通道的请求会被 `body_transform` 注入官方 system 三块 + 日期轮 +
    attribution blob——2026-11-03 线上标定：这笔固定开销 ≈400 tok，count_tokens
    若只算客户端原始 body，对小请求低估近百倍（6 tok 的请求实际 341）。
    attribution 用定长代表值（实际 device_mid/session_id 同量级，差 <1 tok）。
    Key 回退通道不做 transform，此估算对其略偏高——预算场景偏高是安全方向。
    """
    from . import body_transform

    transformed = copy.deepcopy(body)
    body_transform.transform_body(transformed, None, model,
                                  device_mid="0" * 32, session_id="0" * 36)
    return estimate_input_tokens(transformed)
