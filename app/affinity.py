"""会话上下文与亲和性管理（Session Affinity）。

核心诉求：
1. 多账号负载均衡：新来的请求必须均匀分散到不同可用账号（最小在飞优先，轮询打破僵局）。
2. 会话亲和性：属于同一请求上下文的多轮对话粘性路由到同一账号，保持上游 Prompt Cache 与会话局部性。
3. 满载与故障溢出：若亲和账号并发达上限（写死 2）或进入冷却/禁用，自动溢出到其他可用账号并更新绑定。
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, NamedTuple

from . import body_transform

DEFAULT_AFFINITY_TTL = 3600.0  # 1 小时未活跃自动释放
DEFAULT_MAX_ENTRIES = 10000

# 常见客户端下发的会话/对话标识头（小写查找）
_SESSION_HEADER_KEYS = (
    "x-session-id",
    "session-id",
    "x-conversation-id",
    "conversation-id",
    "x-chat-id",
    "chat-id",
)


class ContextInfo(NamedTuple):
    """请求上下文提取结果。"""
    key: str | None
    is_continuation: bool


class SessionAffinity:
    """线程安全的会话亲和性缓存，维护 context_key -> (account_id, expire_at)。"""

    def __init__(self, ttl: float = DEFAULT_AFFINITY_TTL, max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, key: str | None) -> str | None:
        """获取亲和绑定的 account_id。若命中则续期（滑动窗口），过期则清理并返回 None。"""
        if not key:
            return None
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            account_id, expire_at = entry
            if now > expire_at:
                self._cache.pop(key, None)
                return None
            # 命中续期
            self._cache[key] = (account_id, now + self._ttl)
            return account_id

    def has(self, key: str | None) -> bool:
        """检查是否存在有效绑定（不触发滑动窗口续期）。"""
        if not key:
            return False
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return False
            if now > entry[1]:
                self._cache.pop(key, None)
                return False
            return True

    def bind(self, key: str | None, account_id: str) -> None:
        """绑定 context_key 到 account_id，若已存在则更新。"""
        if not key or not account_id:
            return
        now = time.time()
        with self._lock:
            if len(self._cache) >= self._max_entries:
                self._cleanup_locked(now)
            self._cache[key] = (account_id, now + self._ttl)

    def remove(self, key: str | None) -> None:
        """主动解绑指定 key。"""
        if not key:
            return
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        """清空亲和性缓存。"""
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        """返回当前缓存大小（包含未过期的条目）。"""
        with self._lock:
            return len(self._cache)

    def _cleanup_locked(self, now: float) -> None:
        # 1. 清理过期条目
        expired = [k for k, (_, exp) in self._cache.items() if now > exp]
        for k in expired:
            self._cache.pop(k, None)
        # 2. 若依然超过最大容量，按过期时间升序淘汰最老的 20%
        if len(self._cache) >= self._max_entries:
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][1])
            drop_count = max(1, len(sorted_keys) // 5)
            for k in sorted_keys[:drop_count]:
                self._cache.pop(k, None)


# 全局单例
affinity_manager = SessionAffinity()


def _extract_header_session_id(headers: dict[str, Any] | None) -> str | None:
    if not headers:
        return None
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() in _SESSION_HEADER_KEYS:
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _extract_meta_session_id(body: dict[str, Any]) -> str | None:
    meta = body.get("metadata")
    if isinstance(meta, dict):
        sid = meta.get("session_id") or meta.get("conversation_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    # 根层兼容
    root_sid = body.get("session_id") or body.get("conversation_id")
    if isinstance(root_sid, str) and root_sid.strip():
        return root_sid.strip()
    return None


def _has_assistant_turn(messages: list[Any]) -> bool:
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return True
    return False


def extract_context_key(
    body: dict[str, Any],
    headers: dict[str, Any] | None = None,
    provider: str = "zai",
    manager: SessionAffinity = affinity_manager,
) -> ContextInfo:
    """提取会话上下文 key 并判断是否为同一上下文的延续请求。

    规则：
    1. 优先提取显式请求头（x-session-id 等）：
       - 若此前已绑定过账号且未过期，或请求消息含 assistant 轮：视为延续请求（is_continuation=True）。
       - 若从未绑定过且无 assistant 轮：视为新会话的首轮（is_continuation=False）。
    2. 其次提取请求体中的 metadata.session_id：
       - 规则同上。
    3. 若无显式标识，检查消息历史：
       - 若包含 assistant 轮次，说明已进入多轮往返：
         提取首条有效 user 文本（跳过日期注入轮），结合客户端 IP 计算特征指纹；
         视为延续请求（is_continuation=True）。
       - 若无 assistant 轮次（仅单条或仅 user 轮）：
         提取首条有效 user 文本并生成指纹，但作为新请求对待（is_continuation=False），
         从而保证独立的单轮请求（如多次打 "hello"）均匀分流到不同账号，并在首轮成功后记录指纹
         供后续第 2 轮匹配。
    """
    messages = body.get("messages")
    has_history = isinstance(messages, list) and _has_assistant_turn(messages)

    # 1. 显式请求头
    hdr_id = _extract_header_session_id(headers)
    if hdr_id:
        key = f"{provider}:hdr:{hdr_id}"
        is_cont = has_history or manager.has(key)
        return ContextInfo(key=key, is_continuation=is_cont)

    # 2. 元数据 session_id
    meta_id = _extract_meta_session_id(body)
    if meta_id:
        key = f"{provider}:meta:{meta_id}"
        is_cont = has_history or manager.has(key)
        return ContextInfo(key=key, is_continuation=is_cont)

    # 3. 消息指纹（首轮有效用户文本 + 客户端 IP）
    first_text = body_transform.extract_first_user_text(body)
    if not first_text:
        return ContextInfo(key=None, is_continuation=False)

    client_ip = "local"
    if headers:
        norm = {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
        raw_ip = norm.get("cf-connecting-ip") or norm.get("x-forwarded-for")
        if isinstance(raw_ip, str) and raw_ip.strip():
            client_ip = raw_ip.split(",")[0].strip()

    digest = hashlib.sha256(first_text.encode("utf-8")).hexdigest()[:32]
    key = f"{provider}:conv:{client_ip}:{digest}"

    # 若含有 assistant 轮，则是明确的延续上下文；若缓存已有且未过期也是延续；否则是新请求
    is_cont = has_history or manager.has(key)
    return ContextInfo(key=key, is_continuation=is_cont)
