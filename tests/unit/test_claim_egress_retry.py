"""claim IP 信誉驱动换绑重试链单元测试（2026-11-16 实弹定案）。

背景：同批账号同一套餐，唯一变量是 TP 出口节点——7/11 过、4 败全为
403 unusual activity / 求解失败。结论：成败强跟随出口 IP 在阿里云/billing
侧的信誉。本链路：信誉型失败 → ban 旧节点换绑 → 验证码现解验收 → 重打。
"""

from __future__ import annotations

import asyncio

import pytest

from app import claim, settings
from app.captcha import CaptchaSolveError
from app.claim import ClaimError, claim_with_egress_retry
from app.models import Account


def _account(acc_id="acc-retry", name="retry1", node="node-old"):
    return Account(id=acc_id, name=name, provider="zai", mode="jwt",
                   jwt_token="h.eyJzdWIiOiJ1In0.sig", egress_node=node)


@pytest.fixture
def tp_mode(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    monkeypatch.setattr(settings, "TP_GATEWAY", "127.0.0.1:9101")
    monkeypatch.setattr(settings, "TP_TYPE", "clash_proxy")
    monkeypatch.setattr(settings, "TP_REGION", "us-west")
    monkeypatch.setattr(settings, "TP_TIER", "standard")
    monkeypatch.setattr(settings, "TP_SERVICE", "zcode-hub")
    monkeypatch.setattr(settings, "TP_REBALANCE_DEBOUNCE", 0)


class _FakeEgressPool:
    def __init__(self, *, ok=True, new_node="node-new"):
        self.calls: list[tuple] = []
        self._ok = ok
        self._new_node = new_node

    async def rebalance(self, account, reason, ban=True):
        self.calls.append((account.name, reason, ban))
        if self._ok:
            account.egress_node = self._new_node
        return self._ok


# ── 归因判定 ────────────────────────────────────────────────────────────────
def test_reputation_failures_include_403_3007_and_solve_error():
    # 403 必须风控文案实锤（2.7.50 review P1：文案盲信会白烧换绑循环）
    assert claim._is_egress_reputation_failure(
        ClaimError("上游风控拒绝 HTTP 403（非凭证失效）: unusual activity", code=403))
    assert claim._is_egress_reputation_failure(
        ClaimError('"code":3012 ...', code=403))
    assert claim._is_egress_reputation_failure(ClaimError("验证码拒", code=3007))


def test_403_without_risk_marker_not_retried():
    # 文案漂移/未知 403 语义：换 IP 无解，盲目换绑白烧求解与写流量
    assert not claim._is_egress_reputation_failure(ClaimError("未知 403", code=403))


def test_non_reputation_claim_errors_not_retried():
    # 套餐/名额/参数/凭证维度：换 IP 无用
    for code in (1001, 1002, 1003, 1004, 1005, 3001, 3002, 401, -1):
        assert not claim._is_egress_reputation_failure(ClaimError("x", code=code))


def test_captcha_solve_error_is_reputation_failure_by_name():
    # 延迟导入避免 app.claim 顶层依赖 captcha.CaptchaSolveError 的循环
    assert claim._is_egress_reputation_failure(CaptchaSolveError("F001"))
    # 同形异名（如回滚路径的其他求解错误）不重试
    class FakeSolveError(Exception):
        pass
    assert not claim._is_egress_reputation_failure(FakeSolveError("boom"))


# ── _rebalance_for_claim ────────────────────────────────────────────────────
def test_rebalance_requires_per_account_proxy(monkeypatch, tp_mode):
    acc = _account()
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", False)
    assert asyncio.run(claim._rebalance_for_claim(acc, "403 unusual")) is False


def test_rebalance_ok_then_captcha_acceptance_passes(monkeypatch, tp_mode):
    acc = _account()
    pool = _FakeEgressPool()
    monkeypatch.setattr(claim, "egress_pool", pool, raising=False)
    invalidated: list = []
    solved: list[dict] = []
    egress_proxy_url = None
    from app import egress as egress_mod
    egress_proxy_url = egress_mod.account_proxy(acc)

    # claim.py 内 egress_pool 是函数内延迟导入：patch 真实模块单例
    from app import egress_pool as ep_mod
    monkeypatch.setattr(ep_mod, "egress_pool", pool)
    monkeypatch.setattr(claim.captcha_manager, "invalidate",
                        lambda tag: invalidated.append(tag))
    async def fake_solve(egress_tag=None, proxy=None, retries=None):
        solved.append({"tag": egress_tag, "proxy": proxy, "retries": retries})
        return "param-x"
    monkeypatch.setattr(claim.captcha_manager, "get_verify_param", fake_solve)

    assert asyncio.run(claim._rebalance_for_claim(acc, "403 unusual")) is True
    assert acc.egress_node == "node-new"
    # TP ban reason 是闭合枚举（TetherPool API 需求 §ban），必须用枚举值；
    # 且必须是 edge_405_html——unusual_activity_3012 被 TP 服务端强制
    # quarantine=0（只记录不隔离），captcha 信誉 ban 传它等于白 ban
    # （2026-11-17 实弹：当晚 8 条「ban 已隔离」实际全部未隔离）
    assert pool.calls == [("retry1", "edge_405_html", True)]
    # 旧 tag（PROXY 模式下即网关代理 URL）在换绑前被作废，避免坏 IP 池内码复用
    assert invalidated == [egress_proxy_url]
    assert len(solved) == 1 and solved[0]["proxy"]


def test_rebalance_rejects_noop_node_change(monkeypatch, tp_mode):
    acc = _account(node="node-old")
    # rebalance 成功但节点没变（同节点重绑）→ 不值得重打
    pool = _FakeEgressPool(ok=True, new_node="node-old")
    from app import egress_pool as ep_mod
    monkeypatch.setattr(ep_mod, "egress_pool", pool)
    monkeypatch.setattr(claim.captcha_manager, "invalidate", lambda tag: None)

    assert asyncio.run(claim._rebalance_for_claim(acc, "3007")) is False


def test_rebalance_rejects_acceptance_failure(monkeypatch, tp_mode):
    acc = _account()
    pool = _FakeEgressPool()
    from app import egress_pool as ep_mod
    monkeypatch.setattr(ep_mod, "egress_pool", pool)
    monkeypatch.setattr(claim.captcha_manager, "invalidate", lambda tag: None)
    async def bad_solve(egress_tag=None, proxy=None):
        raise CaptchaSolveError("F001")
    monkeypatch.setattr(claim.captcha_manager, "get_verify_param", bad_solve)

    # 新节点上验证码仍被阿里云拒 = 信誉没恢复，不重打
    assert asyncio.run(claim._rebalance_for_claim(acc, "403")) is False


# ── claim_with_egress_retry ─────────────────────────────────────────────────
def test_retry_success_after_rebalance(monkeypatch, tp_mode):
    acc = _account()
    calls = {"claim": 0, "rebalance": 0}
    sleeps: list[float] = []

    async def fake_claim(account, plan_id=None):
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise ClaimError("HTTP 403 unusual activity", code=403)
        return {"ok": True, "plan": plan_id}

    async def fake_rebalance(account, reason):
        calls["rebalance"] += 1
        account.egress_node = "node-new"
        return True

    monkeypatch.setattr(claim, "claim", fake_claim)
    monkeypatch.setattr(claim, "_rebalance_for_claim", fake_rebalance)

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(claim.asyncio, "sleep", fake_sleep)

    out = asyncio.run(claim_with_egress_retry(acc, "plan-1116"))
    assert out == {"ok": True, "plan": "plan-1116"}
    assert calls == {"claim": 2, "rebalance": 1}
    assert sleeps == [claim._EGRESS_RETRY_DELAY_S]


def test_non_reputation_error_raises_immediately(monkeypatch, tp_mode):
    acc = _account()
    calls = {"claim": 0, "rebalance": 0}

    async def fake_claim(account, plan_id=None):
        calls["claim"] += 1
        raise ClaimError("套餐不存在", code=1001)

    async def fake_rebalance(account, reason):
        calls["rebalance"] += 1
        return True

    monkeypatch.setattr(claim, "claim", fake_claim)
    monkeypatch.setattr(claim, "_rebalance_for_claim", fake_rebalance)

    with pytest.raises(ClaimError) as ei:
        asyncio.run(claim_with_egress_retry(acc, "plan-x"))
    assert ei.value.code == 1001
    assert calls == {"claim": 1, "rebalance": 0}  # 未换绑、未重打


def test_retry_budget_exhausted_raises_last_error(monkeypatch, tp_mode):
    acc = _account()
    monkeypatch.setattr(claim, "CLAIM_EGRESS_RETRIES", 1)
    attempts: list[int] = []

    async def fake_claim(account, plan_id=None):
        attempts.append(1)
        raise ClaimError("风控 unusual activity", code=403)

    async def fake_rebalance(account, reason):
        account.egress_node = "n2"
        return True

    monkeypatch.setattr(claim, "claim", fake_claim)
    monkeypatch.setattr(claim, "_rebalance_for_claim", fake_rebalance)

    async def fake_sleep(s):
        pass
    monkeypatch.setattr(claim.asyncio, "sleep", fake_sleep)

    with pytest.raises(ClaimError):
        asyncio.run(claim_with_egress_retry(acc, "plan-x"))
    assert len(attempts) == 2  # 1 次原始 + 1 次重试（RETRIES=1）


def test_rebalance_failure_reraises_original_error(monkeypatch, tp_mode):
    acc = _account()
    original = ClaimError("风控", code=3007)

    async def fake_claim(account, plan_id=None):
        raise original

    async def fake_rebalance(account, reason):
        return False  # 换绑未生效

    monkeypatch.setattr(claim, "claim", fake_claim)
    monkeypatch.setattr(claim, "_rebalance_for_claim", fake_rebalance)

    with pytest.raises(ClaimError) as ei:
        asyncio.run(claim_with_egress_retry(acc, "plan-x"))
    assert ei.value is original


# ── tp_ban reason 归一（TP reason 闭合枚举，自由文本 422）────────────────────
def test_tp_ban_reason_slugged_to_ascii(monkeypatch, tp_mode):
    """tp_ban 对非 ASCII/冒号 reason 做 slug 归一，防 TP 422（2.7.50 实弹实证）。"""
    import asyncio as _aio

    from app import egress_pool as ep

    captured: dict = {}

    def fake_http(method, url, body, headers, timeout):
        captured["body"] = __import__("json").loads(body)
        return 200, '{"ok":true}', {}

    monkeypatch.setattr(ep, "_tp_http", fake_http)
    _aio.run(ep.tp_ban("node-x", "claim-egress:验证码求解失败: 多次重试无结果", {}, "a1"))
    reason = captured["body"]["reason"]
    assert reason == "claim-egress"  # 非法字符全折叠为 -，首尾 - 剥离
    assert reason.isascii()


# ── 2.7.50 深度迭代（review P2/P4）───────────────────────────────────────────
def test_rebalance_internal_error_swallowed_original_reraised(monkeypatch, tp_mode):
    """换绑环节自身异常（store/probe 等）被吞，原始 claim 错误照常上抛。"""
    acc = _account()
    original = ClaimError("上游风控拒绝 HTTP 403: unusual activity", code=403)

    async def fake_claim(account, plan_id=None):
        raise original

    async def boom_rebalance(account, reason):
        raise RuntimeError("sqlite disk I/O error")

    monkeypatch.setattr(claim, "claim", fake_claim)
    monkeypatch.setattr(claim, "_rebalance_for_claim", boom_rebalance)

    with pytest.raises(ClaimError) as ei:
        asyncio.run(claim_with_egress_retry(acc, "plan-x"))
    assert ei.value is original  # 不是 RuntimeError 穿透


def test_rebalance_for_claim_internal_error_returns_false(monkeypatch, tp_mode):
    """_rebalance_for_claim 内部任何异常 → False（不外泄），归因留日志。"""
    acc = _account()

    from app import egress_pool as ep_mod

    class _BoomPool:
        async def rebalance(self, account, reason, ban=True):
            raise RuntimeError("tp http boom")

    monkeypatch.setattr(ep_mod, "egress_pool", _BoomPool())
    assert asyncio.run(claim._rebalance_for_claim(acc, "403")) is False


def test_captcha_solve_error_isinstance_not_string_match():
    """isinstance 化（P4）：改名不再静默失效。"""
    err = CaptchaSolveError("x")
    assert type(err).__name__ == "CaptchaSolveError"
    assert claim._is_egress_reputation_failure(err)


def test_acceptance_failure_breaks_plan_loop(monkeypatch, tp_mode):
    """验收失败（CaptchaSolveError 上抛）后本账号不再打第二个套餐（P4 止损）。"""
    from app.claim import auto_claim_all_plans

    acc = _account()
    attempts: list[str] = []

    async def fake_claim(account, plan_id=None):
        attempts.append(plan_id)
        raise CaptchaSolveError("验证码求解失败: slidex 求解未果（F001）")

    monkeypatch.setattr(claim, "claim", fake_claim)

    async def fake_preview(account):
        return [{"plan_id": "p1", "name": "P1", "priority": 2, "grants": []},
                {"plan_id": "p2", "name": "P2", "priority": 1, "grants": []}]

    monkeypatch.setattr(claim, "preview_plans", fake_preview)

    outcomes = asyncio.run(auto_claim_all_plans(acc))
    assert attempts == ["p1"]  # p2 不再烧码
    assert all(not o["ok"] for o in outcomes)


def test_403_claim_error_breaks_plan_loop(monkeypatch, tp_mode):
    """2.7.68：403（unusual activity 文案实锤）与 3007 同级止损——多套餐投放期
    403 后不再打第二个套餐（同坏出口白烧一枚码）。"""
    from app.claim import auto_claim_all_plans

    acc = _account()
    attempts: list[str] = []

    async def fake_claim(account, plan_id=None):
        attempts.append(plan_id)
        raise ClaimError("上游风控拒绝 HTTP 403（非凭证失效）: unusual activity", code=403)

    monkeypatch.setattr(claim, "claim", fake_claim)

    async def fake_preview(account):
        return [{"plan_id": "p1", "name": "P1", "priority": 2, "grants": []},
                {"plan_id": "p2", "name": "P2", "priority": 1, "grants": []}]

    monkeypatch.setattr(claim, "preview_plans", fake_preview)

    outcomes = asyncio.run(auto_claim_all_plans(acc))
    assert attempts == ["p1"]  # 403 即止损，p2 不烧
    assert all(not o["ok"] for o in outcomes)


def test_claim_passes_claim_retries_budget(monkeypatch, tp_mode):
    """2.7.68 求解预算解耦：claim() 给 get_verify_param 传
    CAPTCHA_CLAIM_SOLVE_RETRIES（而非 messages 全量 4 次）。"""
    import app.claim as claim_module
    from app import settings as _settings

    acc = _account()
    acc.jwt_token = "x.y.z"
    acc.mode = "jwt"
    seen: dict = {}

    async def fake_gvp(egress_tag=None, proxy=None, retries=None):
        seen["retries"] = retries
        return "param-x", "cn"

    async def fake_config(egress_tag=None, proxy=None):
        return {"region": "cn"}

    async def fake_billing(account, method, path, **kwargs):
        return {"code": 0, "data": {"plan": {"name": "P"}}}

    monkeypatch.setattr(claim_module.captcha_manager, "get_verify_param", fake_gvp)
    monkeypatch.setattr(claim_module.captcha_manager, "fetch_config", fake_config)
    monkeypatch.setattr(claim_module, "_billing_request", fake_billing)

    asyncio.run(claim_module.claim(acc, "p1"))
    assert seen["retries"] == _settings.CAPTCHA_CLAIM_SOLVE_RETRIES
