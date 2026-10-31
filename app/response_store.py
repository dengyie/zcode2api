"""previous_response_id 状态存储 —— /v1/responses 专用内存 LRU。

OpenAI Responses 协议的多轮续接靠 previous_response_id 引用上一轮的
input/output 条目；上游 Anthropic 协议是无状态的，网关在两次请求之间把
历史条目暂存在这里，下一轮命中时合并成完整 messages 重放。

设计要点：
- 仅内存（OrderedDict 上限 256 条，超出淘汰最旧），进程重启即清零：
  定位是续接便利而非持久会话，客户端随时可重发全量 input 兜底
- 与 reqlog 同一哲学：避免写库的 IO 放大，丢失可接受、绝不放大请求失败
- 存取均为浅拷贝列表（防调用方原地改写污染库内条目），条目 dict 本体不深拷贝
"""

from __future__ import annotations

import threading
from collections import OrderedDict

_MAX = 256

_lock = threading.Lock()
_store: OrderedDict[str, dict] = OrderedDict()


def save(response_id: str, input_items: list, output_items: list) -> None:
    """保存一轮响应的 input/output 条目（各存浅拷贝列表）。"""
    with _lock:
        if response_id in _store:
            _store.move_to_end(response_id)
        _store[response_id] = {
            "input_items": list(input_items),
            "output_items": list(output_items),
        }
        while len(_store) > _MAX:
            _store.popitem(last=False)  # 淘汰最旧


def get(response_id: str) -> dict | None:
    """取回条目（命中移到队尾刷新 LRU 位次）；未命中返回 None。"""
    with _lock:
        item = _store.get(response_id)
        if item is None:
            return None
        _store.move_to_end(response_id)
        return {
            "input_items": list(item["input_items"]),
            "output_items": list(item["output_items"]),
        }


def clear() -> None:
    with _lock:
        _store.clear()


def size() -> int:
    with _lock:
        return len(_store)
