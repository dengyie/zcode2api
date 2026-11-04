"""SessionAffinity 亲和性缓存与上下文提取单元测试。"""

from __future__ import annotations

import time

from app.affinity import SessionAffinity, extract_context_key


class TestSessionAffinityCache:
    def test_bind_and_get(self):
        aff = SessionAffinity(ttl=10.0)
        aff.bind("key1", "acc_1")
        assert aff.get("key1") == "acc_1"
        assert aff.has("key1") is True

    def test_get_expired(self):
        aff = SessionAffinity(ttl=0.05)
        aff.bind("key1", "acc_1")
        time.sleep(0.06)
        assert aff.get("key1") is None
        assert aff.has("key1") is False

    def test_sliding_window_expiration(self):
        aff = SessionAffinity(ttl=0.1)
        aff.bind("key1", "acc_1")
        time.sleep(0.05)
        # 命中后滑动续期
        assert aff.get("key1") == "acc_1"
        time.sleep(0.06)
        # 若没续期此时会过期，续期了依然有效
        assert aff.get("key1") == "acc_1"

    def test_remove_and_clear(self):
        aff = SessionAffinity(ttl=10.0)
        aff.bind("k1", "a1")
        aff.bind("k2", "a2")
        assert aff.size() == 2
        aff.remove("k1")
        assert aff.get("k1") is None
        assert aff.get("k2") == "a2"
        aff.clear()
        assert aff.size() == 0


class TestExtractContextKey:
    def test_header_first_request(self):
        aff = SessionAffinity(ttl=10.0)
        body = {"messages": [{"role": "user", "content": "hello"}]}
        headers = {"x-session-id": "sess-123"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key == "zai:hdr:sess-123"
        assert info.is_continuation is False

    def test_header_continuation_via_history(self):
        aff = SessionAffinity(ttl=10.0)
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "next turn"},
            ]
        }
        headers = {"x-session-id": "sess-123"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key == "zai:hdr:sess-123"
        assert info.is_continuation is True

    def test_header_continuation_via_bound_cache(self):
        aff = SessionAffinity(ttl=10.0)
        aff.bind("zai:hdr:sess-123", "acc_a")
        body = {"messages": [{"role": "user", "content": "hello"}]}
        headers = {"session-id": "sess-123"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key == "zai:hdr:sess-123"
        assert info.is_continuation is True

    def test_metadata_session_id(self):
        aff = SessionAffinity(ttl=10.0)
        body = {
            "metadata": {"session_id": "meta-456"},
            "messages": [{"role": "user", "content": "hello"}],
        }
        info = extract_context_key(body, {}, provider="zai", manager=aff)
        assert info.key == "zai:meta:meta-456"
        assert info.is_continuation is False

    def test_conversation_fingerprint_case_insensitive_header(self):
        """验证 CF-Connecting-IP / X-Forwarded-For 大小写不敏感提取。"""
        aff = SessionAffinity(ttl=10.0)
        body = {"messages": [{"role": "user", "content": "write code"}]}
        headers = {"CF-Connecting-IP": "8.8.8.8"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key is not None
        assert info.key.startswith("zai:conv:8.8.8.8:")

    def test_conversation_fingerprint_new_request(self):
        aff = SessionAffinity(ttl=10.0)
        body = {"messages": [{"role": "user", "content": "write code"}]}
        headers = {"cf-connecting-ip": "1.2.3.4"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key is not None
        assert info.key.startswith("zai:conv:1.2.3.4:")
        assert info.is_continuation is False

    def test_conversation_fingerprint_with_assistant_turn(self):
        aff = SessionAffinity(ttl=10.0)
        body = {
            "messages": [
                {"role": "user", "content": "write code"},
                {"role": "assistant", "content": "def foo(): pass"},
                {"role": "user", "content": "explain it"},
            ]
        }
        headers = {"cf-connecting-ip": "1.2.3.4"}
        info = extract_context_key(body, headers, provider="zai", manager=aff)
        assert info.key is not None
        assert info.key.startswith("zai:conv:1.2.3.4:")
        assert info.is_continuation is True
