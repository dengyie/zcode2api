"""上游请求形状守卫（2026-11-02 · 2.7.6）。

3012「unusual activity」的根因是 system **块结构**：官方 harness 恰好 3 块，
外来 system 文本只许并入最后一块内部。本文件把这条规则钉成回归线——
以后任何改动只要让块数 ≠ 3 或把外来文本拆成独立块，测试立刻红。
"""

from __future__ import annotations

from app import body_transform
from app.models import Account


def _body(system=None, text="hi") -> dict:
    body: dict = {
        "model": "GLM-5.3",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }
    if system is not None:
        body["system"] = system
    return body


class TestSystemShape:
    def test_exactly_three_blocks(self):
        body = _body()
        body_transform.transform_body(body)
        assert len(body["system"]) == 3, "官方 harness 必须是三块，多一块即 3012"

    def test_first_block_is_cli_prefix(self):
        body = _body()
        body_transform.transform_body(body)
        assert body["system"][0]["text"].startswith("You are ZCode")

    def test_user_system_merged_into_last_block_not_new_block(self):
        body = _body(system=[{"type": "text", "text": "守则甲"},
                             {"type": "text", "text": "守则乙"}])
        body_transform.transform_body(body)
        blocks = body["system"]
        assert len(blocks) == 3
        last = blocks[-1]["text"]
        assert "守则甲" in last and "守则乙" in last
        # 不得有独立块只包含用户文本
        assert not any(b["text"].strip() == "守则甲" for b in blocks)

    def test_string_system_is_merged(self):
        body = _body(system="只回复 JSON")
        body_transform.transform_body(body)
        assert len(body["system"]) == 3
        assert "只回复 JSON" in body["system"][-1]["text"]

    def test_powered_by_lives_in_last_block(self):
        body = _body()
        body_transform.transform_body(body, model="GLM-5.3")
        blocks = body["system"]
        assert len(blocks) == 3
        assert "powered by the model named GLM-5.3" in blocks[-1]["text"]

    def test_transform_is_idempotent(self):
        body = _body(system="守则")
        body_transform.transform_body(body)
        first = [b["text"] for b in body["system"]]
        body_transform.transform_body(body)
        assert [b["text"] for b in body["system"]] == first
        assert len(body["system"]) == 3

    def test_all_blocks_carry_cache_control(self):
        body = _body()
        body_transform.transform_body(body)
        assert all(b.get("cache_control") == {"type": "ephemeral"} for b in body["system"])

    def test_date_turn_prepended_once(self):
        body = _body()
        body_transform.transform_body(body)
        assert len(body["messages"]) == 2
        assert "Today's date is" in body["messages"][0]["content"][0]["text"]
        before = len(body["messages"])
        body_transform.transform_body(body)
        # 日期轮已在首位 → 不再重复插入
        assert len(body["messages"]) == before

    def test_metadata_blob_shape(self):
        body = _body()
        body_transform.transform_body(body, device_mid="mid-1", session_id="sid-1")
        import json
        blob = json.loads(body["metadata"]["user_id"])
        assert blob == {"device_id": "mid-1", "account_uuid": "", "session_id": "sid-1"}


class TestConversationSessionId:
    def test_stable_for_same_first_turn(self):
        a = _body(text="把 README 改成中文")
        b = _body(text="把 README 改成中文")
        assert body_transform.conversation_session_id(a, "acc-1") == \
            body_transform.conversation_session_id(b, "acc-1")

    def test_differs_across_conversations(self):
        a = _body(text="会话一")
        b = _body(text="会话二")
        assert body_transform.conversation_session_id(a, "acc-1") != \
            body_transform.conversation_session_id(b, "acc-1")

    def test_differs_across_accounts(self):
        a = _body(text="同一个提示词")
        assert body_transform.conversation_session_id(a, "acc-1") != \
            body_transform.conversation_session_id(a, "acc-2")

    def test_none_without_user_text(self):
        assert body_transform.conversation_session_id({"messages": []}) is None
        assert body_transform.conversation_session_id({"messages": [{"role": "assistant", "content": "x"}]}) is None

    def test_multiturn_keeps_first_turn_id(self):
        first = _body(text="首轮")
        body_transform.transform_body(first)
        # 模拟第二轮：客户端回放历史（含首轮）
        second = {
            "model": "GLM-5.3",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "首轮"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "回答"}]},
                {"role": "user", "content": [{"type": "text", "text": "追问"}]},
            ],
        }
        assert body_transform.conversation_session_id(second, "acc-1") == \
            body_transform.conversation_session_id(_body(text="首轮"), "acc-1")


class TestPlanKind:
    """套餐类型判定（后台区分体验号/付费号用）。

    注意：不给 start-plan 单独压并发。2026-11-02 实测同一 trial JWT
    2 并发 + 3 连发全部 200，3009 是探测抖动而非套餐并发配额；
    按套餐硬压到 1 会无端把并行请求打成 503。
    """

    def _acc(self, plan_id: str) -> Account:
        acc = Account.create("zai", "t", "a.b.c")
        acc.plans = [{"plan_id": plan_id, "name": plan_id}]
        acc.plan = acc.plans[0]
        return acc

    def test_start_plan(self):
        assert self._acc("zcode-v3-start-plan-0817").plan_kind() == "start-plan"

    def test_coding_plan(self):
        assert self._acc("zcode-v3-coding-plan-0817").plan_kind() == "coding-plan"

    def test_unknown(self):
        assert Account.create("zai", "t", "a.b.c").plan_kind() == ""


class TestSessionIdUnderRetries:
    """重试/换号路径的指纹稳定性（2026-11-03 复现的缺陷）。

    `transform_body` 原地把日期轮插到 messages[0]，而 gateway 会对**同一个
    body 对象**在验证码重试 / 429 重试 / 换号时再次调用 `build_request` →
    `conversation_session_id`。指纹若落在日期文本上，同一账号同一天的所有
    会话会撞成同一个 id（跨会话关联信号 + cache 键漂移）。
    """

    def test_id_stable_after_transform(self):
        body = _body(text="帮我写个脚本")
        before = body_transform.conversation_session_id(body, "acc-1")
        body_transform.transform_body(body)  # 日期轮已前插
        assert body_transform.conversation_session_id(body, "acc-1") == before

    def test_distinct_conversations_distinct_after_transform(self):
        a, b = _body(text="会话甲"), _body(text="会话乙")
        body_transform.transform_body(a)
        body_transform.transform_body(b)
        assert body_transform.conversation_session_id(a, "acc-1") \
            != body_transform.conversation_session_id(b, "acc-1")

    def test_skips_only_leading_date_turns(self):
        """只跳过日期轮本身；客户端真实的首轮文本始终是指纹来源。"""
        body = _body(text="<system-reminder>\nToday's date is 2026-11-03.\n</system-reminder>")
        body_transform.transform_body(body)  # 我们的前插轮在它前面
        # 两个日期形态轮都被跳过后没有真实文本 → None（而不是把日期当指纹）
        assert body_transform.conversation_session_id(body, "acc-1") is None

    def test_date_turn_helper_is_single_source(self):
        assert body_transform._is_date_turn_text(
            "<system-reminder>\nToday's date is 2026-11-03.\n</system-reminder>")
        assert not body_transform._is_date_turn_text("<system-reminder>别的提醒</system-reminder>")
        assert not body_transform._is_date_turn_text(None)
