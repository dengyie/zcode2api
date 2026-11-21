"""ClaimRoundManager 单测：门控过滤、1005 退避、停服截止。

不跑真实 loop（睡眠周期），直接驱动 _round() / stop() 验证：
- 仅 allows_billing() 的 JWT 账号进入轮次（无 JWT / 废 JWT 之外一律进；
  冷却/停用/风控禁用不再排除——领取与使用两条链，2026-11-04 口径）
- 有新套餐入账才触发额度刷新
- 1005+next_at 的套餐在等待期跳过领取（preview 照常、不耗 captcha），
  next_at 过后恢复重试，成功/其余失败清除退避
- stop() 对轮内长尾有截止并 cancel，停服不被卡住
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.claim import ClaimRoundManager


class _FakeAccount:
    def __init__(self, aid: str, *, billing: bool):
        self.id = aid
        self.name = f"acct-{aid}"
        self.mode = "jwt"
        self._billing = billing

    def allows_billing(self) -> bool:
        return self._billing


def _fake_store(accounts):
    class _FakeStore:
        @staticmethod
        def list_accounts(_provider):
            return accounts

        @staticmethod
        def find(_provider, aid):
            return next(a for a in accounts if a.id == aid)

    return _FakeStore()


@pytest.mark.asyncio
async def test_round_only_visits_billing_allowed_accounts(monkeypatch):
    visited: list[str] = []
    refreshed: list[str] = []
    accounts = [
        _FakeAccount("ok-1", billing=True),
        _FakeAccount("no-jwt", billing=False),
        _FakeAccount("invalid", billing=False),
        _FakeAccount("ok-2", billing=True),
    ]

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        visited.append(acc.id)
        return [{"account_id": acc.id, "ok": acc.id == "ok-1"}] if acc.id == "ok-1" else []

    async def fake_refresh(batch):
        refreshed.extend(a.id for a in batch)
        return {"ok": len(batch), "fail": 0}

    import app.claim as claim_module

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    monkeypatch.setattr("app.quota.refresh_accounts", fake_refresh)
    monkeypatch.setattr("app.store.store", _fake_store(accounts))

    manager = ClaimRoundManager()
    await manager._round()

    assert visited == ["ok-1", "ok-2"], "无 JWT / 废 JWT 账号必须先被 allows_billing 过滤"
    assert refreshed == ["ok-1"], "仅真正领到套餐的账号触发额度刷新"


@pytest.mark.asyncio
async def test_round_swallows_single_account_error(monkeypatch):
    accounts = [_FakeAccount("boom", billing=True), _FakeAccount("fine", billing=True)]
    visited: list[str] = []

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        visited.append(acc.id)
        if acc.id == "boom":
            raise RuntimeError("captcha exploded")
        return [{"account_id": acc.id, "ok": True}]

    async def fake_refresh(batch):
        return {"ok": len(batch), "fail": 0}

    import app.claim as claim_module

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    monkeypatch.setattr("app.quota.refresh_accounts", fake_refresh)
    monkeypatch.setattr("app.store.store", _fake_store(accounts))

    manager = ClaimRoundManager()
    await manager._round()  # 不应抛出
    assert visited == ["boom", "fine"], "单账号异常不中断整轮"


@pytest.mark.asyncio
async def test_round_1005_next_at_holds_then_clears_on_success(monkeypatch):
    """1005 退避全周期：名额等待期跳过领取 → next_at 过后恢复 → 成功清除退避。"""
    accounts = [_FakeAccount("a1", billing=True)]
    skip_sets: list[set[str]] = []
    refreshed: list[str] = []
    quota_full = {"now": True}

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        skip = set(skip_plan_ids or set())
        skip_sets.append(skip)
        if "p-quota" in skip:
            return []  # 真实行为：跳过的套餐不产生 outcome
        if quota_full["now"]:
            return [{"account_id": acc.id, "account_name": acc.name, "ok": False,
                     "plan_id": "p-quota", "message": "今日领取名额已用完",
                     "code": 1005, "next_at": round((time.time() + 3600) * 1000)}]
        return [{"account_id": acc.id, "account_name": acc.name, "ok": True,
                 "plan_id": "p-quota", "plan_name": "Quota Plan", "grants": []}]

    async def fake_refresh(batch):
        refreshed.extend(a.id for a in batch)
        return {"ok": len(batch), "fail": 0}

    import app.claim as claim_module

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    monkeypatch.setattr("app.quota.refresh_accounts", fake_refresh)
    monkeypatch.setattr("app.store.store", _fake_store(accounts))

    manager = ClaimRoundManager()
    await manager._round()  # 首轮 1005 → 记忆退避
    assert skip_sets[0] == set()
    assert "p-quota" in manager._claim_holds["a1"]
    assert refreshed == [], "1005 失败轮不得触发额度刷新"

    await manager._round()  # 等待期：p-quota 被跳过（preview 照常发现）
    assert skip_sets[1] == {"p-quota"}, "名额等待期必须跳过 claim（省 captcha）"

    # next_at 过后：退避解除，恢复重试并成功 → 退避清除
    manager._claim_holds["a1"]["p-quota"] = round((time.time() - 1) * 1000)
    quota_full["now"] = False
    await manager._round()
    assert skip_sets[2] == set(), "next_at 过后必须恢复重试"
    assert refreshed == ["a1"], "领到套餐立即刷新额度"
    assert manager._claim_holds == {}, "成功领取必须清除退避"


@pytest.mark.asyncio
async def test_round_1005_without_next_at_no_hold(monkeypatch):
    """1005 不带 next_at 时无退避依据，下一轮照常重试（不记忆）。"""
    accounts = [_FakeAccount("a1", billing=True)]
    skip_sets: list[set[str]] = []

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        skip_sets.append(set(skip_plan_ids or set()))
        return [{"account_id": acc.id, "account_name": acc.name, "ok": False,
                 "plan_id": "p-x", "message": "今日领取名额已用完", "code": 1005}]

    async def fake_refresh(batch):
        return {"ok": len(batch), "fail": 0}

    import app.claim as claim_module

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    monkeypatch.setattr("app.quota.refresh_accounts", fake_refresh)
    monkeypatch.setattr("app.store.store", _fake_store(accounts))

    manager = ClaimRoundManager()
    await manager._round()
    await manager._round()
    assert skip_sets == [set(), set()], "无 next_at 不建立退避"
    assert manager._claim_holds == {}


@pytest.mark.asyncio
async def test_stop_cancels_long_tail_round(monkeypatch):
    """停服截止：轮内长尾（验证码求解重试可达分钟级）超时即 cancel。"""
    manager = ClaimRoundManager()

    async def hang(self):
        await asyncio.sleep(60)

    monkeypatch.setattr(ClaimRoundManager, "_round", hang)
    monkeypatch.setattr(ClaimRoundManager, "STOP_GRACE_SECONDS", 0.2)
    manager._task = asyncio.create_task(manager._round())
    await asyncio.sleep(0.05)  # 确保已进入长尾
    task = manager._task

    t0 = time.monotonic()
    await manager.stop()
    assert time.monotonic() - t0 < 5, "stop 必须在截止内返回，不得等完 60s 长尾"
    assert task.cancelled(), "超时后轮任务必须被 cancel"
    assert manager._task is None


@pytest.mark.asyncio
async def test_round_no_accounts_warns_on_state_change_only(fresh_app, monkeypatch):
    """无可服务账号告警只在状态变化时发（2.6.6：持续故障期不逐轮刷屏）。"""
    import app.claim as claim_module

    warns: list[str] = []
    # 只收 claim 标签：logs.warn 是共享模块，version_tracker 等组件的告警会混进来
    monkeypatch.setattr(
        claim_module.logs, "warn",
        lambda tag, msg: warns.append(msg) if tag == "claim" else None,
    )

    async def fake_round_fn(acc, skip_plan_ids=None):
        return []

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_round_fn)
    manager = claim_module.ClaimRoundManager()

    await manager._round()  # 空池 → 告警一次
    await manager._round()  # 持续空 → 不刷屏
    assert len(warns) == 1

    acc = fresh_app.add_account("zai", "back", "h1.eyJzdWIiOiJhIn0.sig")
    await manager._round()  # 恢复有号 → 重置告警状态
    assert len(warns) == 1

    fresh_app.remove_account("zai", acc.id)
    await manager._round()  # 再次全空 → 重新告警
    assert len(warns) == 2


@pytest.mark.asyncio
async def test_auto_claim_generic_exception_appends_outcome(monkeypatch):
    """非 ClaimError 异常（如验证码求解失败）也产生失败 outcome，不再只有日志。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "boom", "h1.eyJzdWIiOiJhIn0.sig")

    async def fake_activation(a):
        return None

    async def fake_preview(a):
        return [{"plan_id": "p-1", "name": "P", "grants": [], "priority": 0}]

    async def fake_claim(a, plan_id=None):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(claim_module, "report_activation_events", fake_activation)
    monkeypatch.setattr(claim_module, "preview_plans", fake_preview)
    monkeypatch.setattr(claim_module, "claim", fake_claim)

    outcomes = await claim_module.auto_claim_all_plans(acc)
    assert outcomes == [{"account_id": acc.id, "account_name": acc.name,
                         "ok": False, "plan_id": "p-1", "message": "solver exploded"}]


@pytest.mark.asyncio
async def test_activation_daily_gate_and_ordering(monkeypatch):
    """激活上报时序（2.6.6 根因修复）：先于 preview（投放资格信号假设下，
    「先 preview 后补上报」会形成当日 preview 恒空 → 永不激活的死锁）；
    每号每天最多成功上报一次（官方 app_launch 节奏）。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "gate", "h1.eyJzdWIiOiJhIn0.sig")
    order: list[str] = []

    async def fake_activation(a):
        order.append("activation")
        return None

    async def fake_preview(a):
        order.append("preview")
        return []

    monkeypatch.setattr(claim_module, "report_activation_events", fake_activation)
    monkeypatch.setattr(claim_module, "preview_plans", fake_preview)
    monkeypatch.setattr(claim_module, "_ACTIVATION_REPORTED", {})

    # 当日首轮：激活先于 preview，且结果记入去重门
    outcomes = await claim_module.auto_claim_all_plans(acc)
    assert outcomes == []
    assert order == ["activation", "preview"]
    assert claim_module._ACTIVATION_REPORTED[acc.id] == claim_module._today_key()

    # 同日第二轮：不再上报，preview 照常（额度发现不中断）
    order.clear()
    outcomes = await claim_module.auto_claim_all_plans(acc)
    assert outcomes == []
    assert order == ["preview"]


@pytest.mark.asyncio
async def test_activation_failure_retries_next_round(monkeypatch):
    """上报失败不记入去重门：下一轮重试（成功才记账），避免瞬时故障吞掉当日激活。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "retry", "h1.eyJzdWIiOiJhIn0.sig")
    calls: list[int] = []

    async def failing_activation(a):
        calls.append(1)
        return "上游 500"

    async def fake_preview(a):
        return []

    monkeypatch.setattr(claim_module, "report_activation_events", failing_activation)
    monkeypatch.setattr(claim_module, "preview_plans", fake_preview)
    monkeypatch.setattr(claim_module, "_ACTIVATION_REPORTED", {})

    await claim_module.auto_claim_all_plans(acc)
    assert calls == [1]
    assert acc.id not in claim_module._ACTIVATION_REPORTED  # 失败不记账

    await claim_module.auto_claim_all_plans(acc)
    assert calls == [1, 1]  # 下一轮重试


@pytest.mark.asyncio
async def test_activation_reports_again_next_day(monkeypatch):
    """跨天重新上报（每日门按本地日期翻转）。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "daily", "h1.eyJzdWIiOiJhIn0.sig")
    calls: list[int] = []

    async def fake_activation(a):
        calls.append(1)
        return None

    async def fake_preview(a):
        return []

    monkeypatch.setattr(claim_module, "report_activation_events", fake_activation)
    monkeypatch.setattr(claim_module, "preview_plans", fake_preview)
    monkeypatch.setattr(claim_module, "_ACTIVATION_REPORTED", {})
    # 模拟昨日已上报
    monkeypatch.setattr(
        claim_module, "_today_key", lambda: "2026-10-01", raising=False
    )
    await claim_module.auto_claim_all_plans(acc)
    assert calls == [1]

    # 日期翻转到今天：门重新放行
    monkeypatch.setattr(
        claim_module, "_today_key", lambda: "2026-10-02", raising=False
    )
    await claim_module.auto_claim_all_plans(acc)
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_auto_claim_skip_plan_ids_skips_claim_only(monkeypatch):
    """skip_plan_ids 只跳过领取：新套餐照常领，被跳过的不打 claim。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "skip", "h1.eyJzdWIiOiJhIn0.sig")
    claimed: list[str] = []

    async def fake_activation(a):
        return None

    async def fake_preview(a):
        return [
            {"plan_id": "p-held", "name": "Held", "grants": [], "priority": 1},
            {"plan_id": "p-new", "name": "New", "grants": [], "priority": 0},
        ]

    async def fake_claim(a, plan_id=None):
        claimed.append(plan_id)
        return {"plan_id": plan_id, "plan_name": plan_id, "grants": [],
                "starts_at": None, "ends_at": None, "server_time": None}

    monkeypatch.setattr(claim_module, "report_activation_events", fake_activation)
    monkeypatch.setattr(claim_module, "preview_plans", fake_preview)
    monkeypatch.setattr(claim_module, "claim", fake_claim)

    outcomes = await claim_module.auto_claim_all_plans(acc, skip_plan_ids={"p-held"})
    assert claimed == ["p-new"], "退避中的套餐不得打 claim"
    assert [o["plan_id"] for o in outcomes] == ["p-new"]
    assert all(o["ok"] for o in outcomes)


def test_activation_err_text_empty_message_uses_class_name():
    """httpx 部分异常 str 为空 → 回退类名（2026-11-03 线上：日志只剩「上报失败: 」）。"""
    import httpx

    import app.claim as claim_module

    err = claim_module._activation_err_text("app_daily_active", httpx.ReadError(""))
    assert err == "激活事件 app_daily_active 上报失败: ReadError"


def test_activation_err_text_keeps_nonempty_detail():
    import app.claim as claim_module

    err = claim_module._activation_err_text(
        "app_launch", RuntimeError("event/report app_launch HTTP 500: boom"))
    assert err == "激活事件 app_launch 上报失败: event/report app_launch HTTP 500: boom"


@pytest.mark.asyncio
async def test_round_budget_skips_remaining_accounts(monkeypatch):
    """2.7.68 轮级 wall-clock 预算：预算耗尽后剩余账号本轮跳过（下拍续领），
    单号长尾不再把整晚窗口吃穿。"""
    import app.claim as claim_module
    from app import settings

    accounts = [_FakeAccount(f"acc-{i}", billing=True) for i in range(4)]
    monkeypatch.setattr("app.store.store", _fake_store(accounts))
    monkeypatch.setattr(settings, "CLAIM_ROUND_BUDGET", 0.05)
    monkeypatch.setattr(settings, "CLAIM_ROUND_CONCURRENCY", 1)

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        await asyncio.sleep(0.08)  # 单号 80ms > 预算 50ms：第 2 个号起必超
        return []

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    visited: list[str] = []

    real_auto = fake_auto_claim

    async def spy_auto(acc, *, skip_plan_ids=None):
        visited.append(acc.id)
        return await real_auto(acc, skip_plan_ids=skip_plan_ids)

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", spy_auto)
    mgr = ClaimRoundManager()
    await mgr._round()
    # 预算 0.05s：串行下第 1 号执行后预算耗尽，后续号全部跳过
    assert len(visited) <= 2


@pytest.mark.asyncio
async def test_round_concurrency_runs_accounts_in_parallel(monkeypatch):
    """2.7.68 账号并发：CLAIM_ROUND_CONCURRENCY=2 时两个慢号并行完成
    （串行 2×0.2s ≈ 0.4s，并行 ≈ 0.2s）。"""
    import app.claim as claim_module
    from app import settings

    accounts = [_FakeAccount("slow-1", billing=True), _FakeAccount("slow-2", billing=True)]
    monkeypatch.setattr("app.store.store", _fake_store(accounts))
    monkeypatch.setattr(settings, "CLAIM_ROUND_BUDGET", 0)
    monkeypatch.setattr(settings, "CLAIM_ROUND_CONCURRENCY", 2)

    inflight = {"n": 0, "max": 0}

    async def fake_auto_claim(acc, *, skip_plan_ids=None):
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await asyncio.sleep(0.2)
        inflight["n"] -= 1
        return []

    monkeypatch.setattr(claim_module, "auto_claim_all_plans", fake_auto_claim)
    mgr = ClaimRoundManager()
    await mgr._round()
    assert inflight["max"] == 2


@pytest.mark.asyncio
async def test_same_account_claim_mutex_serializes(monkeypatch):
    """2.7.68 同号互斥：手动端点 / 领取轮 / 入池任务三条链对同一账号并发时
    串行（双链并发 = captcha 池争抢 + billing 双写 + token IP 漂移）。"""
    import app.claim as claim_module
    from app.models import Account

    acc = Account(id="mx-1", name="mx", provider="zai", mode="jwt", jwt_token="a.b.c")
    inflight = {"n": 0, "max": 0}

    async def fake_locked(account, *, skip_plan_ids=None):
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await asyncio.sleep(0.05)
        inflight["n"] -= 1
        return []

    monkeypatch.setattr(claim_module, "_auto_claim_all_plans_locked", fake_locked)
    await asyncio.gather(claim_module.auto_claim_all_plans(acc),
                         claim_module.auto_claim_all_plans(acc),
                         claim_module.auto_claim_all_plans(acc))
    assert inflight["max"] == 1


def test_today_key_is_beijing_anchored():
    """2.7.68 口径统一：激活日门锚定北京时钟（gmtime(t+8h) 的日期字段），
    与领取窗口同口径——host 换 UTC 不漂。"""
    from app.claim import _today_key

    expected = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    assert _today_key() == expected


@pytest.mark.asyncio
async def test_activation_event_transient_retry(monkeypatch):
    """2026-11-21 高可用迭代：激活上报瞬态网络错误 3 次 2s/4s 阶梯重试。

    08:11 抖动窗 4 号实锤——单发即败 = 当天资格信号缺失（白天无轮要拖到
    19 点才补）。事件端点无 Authorization、上游按 device_mid+日期去重，
    重发幂等安全。仅 TransportError 重试；HTTP 4xx/业务码异常不重试。
    """
    import asyncio
    import uuid

    from app import telemetry
    from app.fingerprint import DeviceProfile

    profile = DeviceProfile(
        platform="linux", arch="x64", os_version="6.8.0-45-generic",
        language="zh-CN", timezone="Asia/Shanghai", screen="1920x1080",
        device_mid=str(uuid.uuid4()),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    posts: list[int] = []

    class FakeClient:
        def __init__(self, *a, **kw):
            self.n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            self.n += 1
            posts.append(self.n)
            if self.n < 3:
                raise httpx.ConnectError("boom")
            return _FakeRes()

    class _FakeRes:
        status_code = 200
        text = ""

        def json(self):
            return {"code": 0}

    monkeypatch.setattr(telemetry.httpx, "AsyncClient", FakeClient)
    await telemetry.post_activation_event(profile, "u1", "app_launch")
    assert posts == [1, 2, 3]  # 3 发：2 败 1 成
    assert sleeps == [2, 4]  # 2s/4s 阶梯


@pytest.mark.asyncio
async def test_activation_event_no_retry_on_http_error(monkeypatch):
    """HTTP 4xx / 业务码异常是服务端明确应答，不重试——重发无意义。"""
    import asyncio
    import uuid

    from app import telemetry
    from app.fingerprint import DeviceProfile

    profile = DeviceProfile(
        platform="linux", arch="x64", os_version="6.8.0-45-generic",
        language="zh-CN", timezone="Asia/Shanghai", screen="1920x1080",
        device_mid=str(uuid.uuid4()),
    )
    posts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleeps.append(s) or _noop())

    class _FakeRes:
        status_code = 500
        text = "oops"

        def json(self):
            return {"code": -1}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            posts.append(1)
            return _FakeRes()

    monkeypatch.setattr(telemetry.httpx, "AsyncClient", FakeClient)
    try:
        await telemetry.post_activation_event(profile, "u1", "app_launch")
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert posts == [1]  # 单发即止
    assert sleeps == []


async def _noop():
    return None


@pytest.mark.asyncio
async def test_billing_get_retry_ladder_five_attempts(monkeypatch):
    """2026-11-21 高可用迭代：billing GET 瞬态重试 3→5 次，阶梯 2/4/8/15s。

    08:10-08:13 出口抖动是分钟级（49 条探针失败实锤），6s 总窗全落抖动窗内
    白烧——总窗 ~29s 吃住抖动尾部。POST 仍单发（写操作重发撞 1003）。
    """
    from app import claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "ladder", "h1.eyJzdWIiOiJhIn0.sig")
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    reqs: list[int] = []

    class FakeClient:
        def __init__(self, *a, **kw):
            self.n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kw):
            self.n += 1
            reqs.append(self.n)
            if self.n < 5:
                raise httpx.ConnectError("boom")
            return _FakeBillingRes()

    class _FakeBillingRes:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"plans": []}}

    monkeypatch.setattr(claim_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(claim_module.egress, "account_proxy", lambda a: None)
    monkeypatch.setattr(claim_module.settings, "BILLING_EGRESS", "direct")

    plans = await claim_module.preview_plans(acc)
    assert plans == []
    # preview = billing/preview GET + marketing/touch GET 两条，各自
    # 4 败 1 成（营销链每次请求独立重试）→ 10 发；阶梯 2 组 2/4/8/15
    assert len(reqs) == 10
    assert reqs[:5] == [1, 2, 3, 4, 5]
    assert sleeps == [2, 4, 8, 15, 2, 4, 8, 15]


@pytest.mark.asyncio
async def test_billing_post_keeps_single_attempt(monkeypatch):
    """POST claim 写操作保持单发：超时后上游可能已入账，重发撞 1003。"""
    from app import claim as claim_module
    from app.models import Account

    acc = Account.create("zai", "post1", "h1.eyJzdWIiOiJhIn0.sig")
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    reqs: list[int] = []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kw):
            reqs.append(1)
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(claim_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(claim_module.egress, "account_proxy", lambda a: None)
    monkeypatch.setattr(claim_module.settings, "BILLING_EGRESS", "direct")

    try:
        await claim_module._billing_request(acc, "POST", "/billing/claim",
                                            headers={"x": "y"}, json={})
        raised = False
    except claim_module.ClaimError:
        raised = True
    assert raised
    assert reqs == [1]  # 单发即止
    assert sleeps == []
