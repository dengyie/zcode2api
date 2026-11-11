"""指纹对齐（Magpie 0.1.57 同源语义）单测：轮次 query-id 与 ToolSearch 别名。

出处（Magpie packages/zcode/index.mjs）：
  - queryOf：x-query-id 由「会话 + 用户轮计数」确定性派生，一次提问带出的
    全部请求（含工具循环）同值；无提问不发。
  - searchless/searchNamed：ToolSearch 改名 ToolSearchTool、剥 defer_loading、
    丢弃 DeferredToolPlaceholder——上游见到原名会把其余工具全藏。
"""

from __future__ import annotations

from app.body_transform import (
    TOOL_PLACEHOLDER,
    TOOL_SEARCH,
    TOOL_SEARCH_ALIAS,
    apply_tool_search_alias,
    conversation_query_id,
    transform_body,
)


def _body(*user_texts: str, extra_messages: list | None = None) -> dict:
    messages = [{"role": "user", "content": t} for t in user_texts]
    return {"model": "GLM-5.3", "messages": messages + (extra_messages or [])}


class TestConversationQueryId:
    def test_same_turn_same_id(self):
        """同一轮的多请求（同消息列表）query-id 一致——官方 attribution 语义。"""
        a = conversation_query_id(_body("hi"), "sid-1")
        b = conversation_query_id(_body("hi"), "sid-1")
        assert a and a == b

    def test_new_turn_new_id(self):
        first = conversation_query_id(_body("hi"), "sid-1")
        second = conversation_query_id(_body("hi", "again"), "sid-1")
        assert first != second

    def test_session_scoped(self):
        assert conversation_query_id(_body("hi"), "s1") != conversation_query_id(_body("hi"), "s2")

    def test_tool_result_turns_do_not_count(self):
        """工具回传轮（tool_result-only）不算提问——工具循环内 id 不漂移。"""
        base = _body("hi")
        with_tools = _body("hi", extra_messages=[
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        ])
        assert conversation_query_id(base, "s") == conversation_query_id(with_tools, "s")

    def test_date_turn_skipped_idempotent(self):
        """transform 反复调用（重试/换号）反复前插日期轮，计数不得漂移。"""
        body = _body("hi", "turn two")
        first = conversation_query_id(body, "s")
        transform_body(body, session_id="s")   # 日期轮前插一次
        second = conversation_query_id(body, "s")
        transform_body(body, session_id="s")   # 再来一次（幂等不重复插）
        third = conversation_query_id(body, "s")
        assert first == second == third

    def test_no_query_returns_none(self):
        assert conversation_query_id({"messages": []}, "s") is None
        assert conversation_query_id({"messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
        ]}, "s") is None


class TestToolSearchAlias:
    def _body_with_tools(self) -> dict:
        return {
            "model": "GLM-5.3",
            "system": "own",
            "tools": [
                {"name": TOOL_SEARCH, "description": "search", "defer_loading": True},
                {"name": "Bash", "description": "run"},
                {"name": TOOL_PLACEHOLDER, "defer_loading": True},
            ],
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": TOOL_SEARCH, "input": {"q": "x"}},
                ]},
            ],
        }

    def test_rename_strip_and_drop(self):
        body = self._body_with_tools()
        assert apply_tool_search_alias(body) is True
        names = [t["name"] for t in body["tools"]]
        assert names == [TOOL_SEARCH_ALIAS, "Bash"]  # 占位工具被丢弃
        assert all("defer_loading" not in t for t in body["tools"])
        calls = [b for m in body["messages"] if m["role"] == "assistant"
                 for b in m["content"] if b.get("type") == "tool_use"]
        assert calls[0]["name"] == TOOL_SEARCH_ALIAS  # 历史里的调用名同步改

    def test_idempotent(self):
        body = self._body_with_tools()
        apply_tool_search_alias(body)
        assert apply_tool_search_alias(body) is False  # 二次调用零改动
        assert all(t["name"] != TOOL_SEARCH for t in body["tools"])

    def test_untouched_body_reports_false(self):
        body = _body("hi")
        assert apply_tool_search_alias(body) is False

    def test_alias_name_conflict_keeps_original(self):
        """客户端自己带同名别名工具时不动（Magpie 同款守卫）。"""
        body = {"tools": [{"name": TOOL_SEARCH}, {"name": TOOL_SEARCH_ALIAS}]}
        assert apply_tool_search_alias(body) is False
        assert [t["name"] for t in body["tools"]] == [TOOL_SEARCH, TOOL_SEARCH_ALIAS]

    def test_build_request_is_sole_apply_site(self):
        """别名唯一应用点 = agent.build_request（全通道）；transform_body 不再重复。"""
        import inspect

        from app.agent import build_request

        assert "apply_tool_search_alias" in inspect.getsource(build_request)
