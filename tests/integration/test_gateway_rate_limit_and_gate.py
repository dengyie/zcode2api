"""429 业务码分流全集 + 边缘封禁断路器（2026-11，对齐 magpie 实证 / ZCode 3.17.2）。

在 2.7.22 基线上补齐的三块：
  - 并发族扩到 3008/3009/3010（2.7.6/2.7.9 只认 3009）——三者同为「没排到」的
    瞬态争用，短退避重试，不算账号故障；
  - 1302/1303「真限流」——与并发相反，原地重试只会加重，账号休息
    RATE_LIMIT_COOLDOWN 秒并下沉 Key 渠道/换号（老逻辑会原地等 60s × 5）；
  - 边缘 405 封 IP 断路器（app/gate.py）——同出口 IP 跑多账号会被边缘整体封
    地址，换账号解决不了，必须全进程暂停 plan 渠道且不惩罚账号。
"""

from __future__ import annotations

import time

import pytest

from app import settings
from app.models import Status

# mock 的 sequences/counters 按凭证前 16 位累计（session 级），每个用例必须用
# 独立凭证，否则前一个用例的计数会让 sequence 索引错位。
_JWT_C8 = "hZ8a1.eyJzdWIiOiJ4In0.sig"
_JWT_C10 = "hZ8b2.eyJzdWIiOiJ4In0.sig"
_JWT_CFB = "hZ8c3.eyJzdWIiOiJ4In0.sig"
_JWT_1302 = "hL1a.eyJzdWIiOiJ4In0.sig"
_JWT_1302B = "hL2b.eyJzdWIiOiJ4In0.sig"
_JWT_1302FB = "hL3c.eyJzdWIiOiJ4In0.sig"
_JWT_1302H = "hL4d.eyJzdWIiOiJ4In0.sig"
_JWT_EDGE = "hE1a.eyJzdWIiOiJ4In0.sig"
_JWT_EDGE_FB = "hE2b.eyJzdWIiOiJ4In0.sig"
_JWT_EDGE_HOLD = "hE3c.eyJzdWIiOiJ4In0.sig"
_JWT_RATE = "hG1a.eyJzdWIiOiJ4In0.sig"
_JWT_TS = "hT4s.eyJzdWIiOiJ4In0.sig"
_JWT_BUSY = "hU5b.eyJzdWIiOiJ4In0.sig"
_JWT_TR = "hV6t.eyJzdWIiOiJ4In0.sig"
_JWT_MIX_PLAN = "hW7m.eyJzdWIiOiJ4In0.sig"
_JWT_MIX_KEY = "hX8k.eyJzdWIiOiJ4In0.sig"
_JWT_1113_PLAN = "hY9p.eyJzdWIiOiJ4In0.sig"
_JWT_1113_KEY = "hZ0q.eyJzdWIiOiJ4In0.sig"

_MSG_BODY = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}
_PLAN_PATH = "/api/v1/zcode-plan/anthropic/v1/messages"
_KEY_PATH = "/api/anthropic/v1/messages"
ADMIN_AUTH = {"Authorization": "Bearer zcode"}  # 默认后台密钥


def _count(mock, cred: str) -> int:
    return mock.state.counters.get(cred[:16], 0)


def _paths(mock, before: int) -> list[str]:
    return [c[1] for c in mock.state.calls[before:] if c[1].endswith("/messages")]


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """屏蔽重试等待（含 3009 短退避的 jitter），并记录每次等待秒数。"""
    from app.routes import gateway as gw

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(gw, "_sleep", fake_sleep)
    return slept


@pytest.mark.integration
class TestConcurrencyFamily:
    """3008/3010 与 3009 同族：短退避重试，不算账号故障。"""

    @pytest.mark.parametrize("scenario,jwt", [
        ("concurrency_3008", _JWT_C8),
        ("concurrency_3010", _JWT_C10),
    ])
    async def test_concurrency_retry_then_success(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep, scenario, jwt
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_CONCURRENCY_WAIT", 0)
        acc = seed_account(fresh_app, jwt, name=f"a-{scenario}")
        mock.state.sequences[jwt[:16]] = [scenario, "ok"]

        before = _count(mock, jwt)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        assert _count(mock, jwt) - before == 2  # 首次 + 1 次短退避重试即成功
        # 短退避节奏（RETRY_429_CONCURRENCY_WAIT + jitter ≤ +2s），不是 60s 频控等待
        assert all(0 <= s <= 3 for s in no_retry_sleep)
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE  # 并发上限不是账号故障
        assert after.cooling_until is None
        assert after.risk_strikes == 0

    async def test_concurrency_exhausted_falls_down_ladder(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep
    ):
        """并发重试烧完预算后下沉 API Key 渠道，不冷却账号、不换号。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_CONCURRENCY_WAIT", 0)
        monkeypatch.setattr(settings, "RETRY_429_TIMES", 1)
        key = "sk-fallback-c8-aaaa"
        acc = seed_account(fresh_app, _JWT_CFB, name="a-c8-fb")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[_JWT_CFB[:16]] = ["concurrency_3008"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        paths = _paths(mock, before)
        assert paths.count(_PLAN_PATH) == 2  # 1 + RETRY_429_TIMES 次短退避
        assert paths.count(_KEY_PATH) == 1
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE


@pytest.mark.integration
class TestRateLimit429:
    """1302/1303 真限流：账号休息 RATE_LIMIT_COOLDOWN 秒，不原地重试。"""

    async def test_1302_cools_account_falls_back_same_request(
        self, gateway_client, fresh_app, monkeypatch
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RATE_LIMIT_COOLDOWN", 30)
        key = "sk-fallback-1302-bbbb"
        acc = seed_account(fresh_app, _JWT_1302FB, name="a-1302-fb")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[_JWT_1302FB[:16]] = ["rate_limit_1302"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # 本请求由 Key 渠道完成
        paths = _paths(mock, before)
        assert paths.count(_PLAN_PATH) == 1  # 真限流不原地重试
        assert paths.count(_KEY_PATH) == 1
        after = fresh_app.find("zai", acc.id)
        # 账号休息中（同 3012 语义：冷却期回退成功不得洗回 active）
        assert after.status == Status.COOLING
        assert after.is_cooling() is True
        assert 25 < after.cooling_until - time.time() <= 30
        assert "1302" in (after.last_error or "")
        assert after.risk_strikes == 0  # 限流不是风控命中

    async def test_1302_without_fallback_cools_and_switches(
        self, gateway_client, fresh_app, monkeypatch
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RATE_LIMIT_COOLDOWN", 30)
        seed_account(fresh_app, _JWT_1302, name="a-1302")
        seed_account(fresh_app, _JWT_1302B, name="b-ok")
        mock.state.sequences[_JWT_1302[:16]] = ["rate_limit_1302"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # B 正常服务
        accounts = {a.name: a for a in fresh_app.list_accounts("zai")}
        assert accounts["a-1302"].status == Status.COOLING
        assert accounts["b-ok"].status == Status.ACTIVE

    async def test_1302_200_bearer_maps_to_429_branch(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """HTTP 200 承载 {"code":1302}：业务码映射进 429 分支，同样休息。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RATE_LIMIT_COOLDOWN", 30)
        acc = seed_account(fresh_app, _JWT_1302H, name="a-1302-200")
        mock.state.sequences[_JWT_1302H[:16]] = ["rate_limit_1302_200"]

        before = _count(mock, _JWT_1302H)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 唯一账号休息 → 不空转
        assert _count(mock, _JWT_1302H) - before == 1  # 不原地重试
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.COOLING
        assert "1302" in (after.last_error or "")


@pytest.mark.integration
class TestEdgeBlockBreaker:
    """边缘 405 封 IP：全进程暂停 plan 渠道，不惩罚账号。"""

    async def test_edge_block_trips_gate_and_blocks_next_request(
        self, gateway_client, fresh_app, monkeypatch, stub_captcha
    ):
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 5)
        acc = seed_account(fresh_app, _JWT_EDGE, name="a-edge")
        mock.state.sequences[_JWT_EDGE[:16]] = ["edge_block_405"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        # 观测到边缘页 → 触发 hold 后换号：唯一账号无 Key 可下沉，最终由 dispatch
        # 兜底统一回执（P2-1 修复后不再区分 upstream_edge_blocked/gate_edge_hold
        # 两种客户端形态，边缘观测详情在服务端日志）
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "gate_edge_hold"
        assert "Retry-After" in res.headers
        # 被封的是出口 IP：账号本身不改状态、不计风控
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE
        assert after.cooling_until is None
        assert after.risk_strikes == 0
        assert gate.edge_remaining() > 0

        # 断路器生效期间第二个请求在本地被拒，不再撞上游、不再吃验证码
        before = len(mock.state.calls)
        solves = stub_captcha.solve_count
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 503
        assert res2.json()["error"]["type"] == "gate_edge_hold"
        assert not _paths(mock, before)
        assert stub_captcha.solve_count == solves  # 不先花一枚真浏览器 token 再被拒

    async def test_edge_block_with_apikey_falls_back(self, gateway_client, fresh_app, monkeypatch):
        """IP 被封时 Key 渠道（api.z.ai）风险面不同，同请求可回退成功。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 5)
        key = "sk-fallback-edge-cccc"
        acc = seed_account(fresh_app, _JWT_EDGE_FB, name="a-edge-fb")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[_JWT_EDGE_FB[:16]] = ["edge_block_405"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        paths = _paths(mock, before)
        assert paths.count(_PLAN_PATH) == 1
        assert paths.count(_KEY_PATH) == 1
        assert gate.edge_remaining() > 0  # 回退成功不解除断路器
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE


@pytest.mark.integration
class TestGateRateBudget:
    """进程级分钟预算（默认关闭；单出口小池部署按需打开）。"""

    async def test_rate_budget_rejects_with_429_and_retry_after(
        self, gateway_client, fresh_app, monkeypatch
    ):
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "GATE_RATE_PER_MIN", 1)
        seed_account(fresh_app, _JWT_RATE, name="a-rate")
        mock.state.sequences[_JWT_RATE[:16]] = ["ok"]

        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 200
        assert gate._inflight == 0  # 请求结束必须归还槽位（随流关闭）

        before = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 429
        assert res2.json()["error"]["type"] == "gate_rate_limit"
        assert 1 <= int(res2.headers["Retry-After"]) <= 60
        assert not _paths(mock, before)  # 本地拦截，不打上游


@pytest.mark.integration
class TestToolSearchAlias:
    """ToolSearch 别名端到端：请求侧改名 + 响应侧还原（Magpie searchNamed）。"""

    async def test_request_renamed_and_response_restored(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _JWT_TS, name="a-ts")
        mock.state.sequences[_JWT_TS[:16]] = ["tool_search_ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY | {"tools": [
            {"name": "ToolSearch", "description": "search tools", "defer_loading": True},
            {"name": "Bash", "description": "run"},
            {"name": "DeferredToolPlaceholder", "defer_loading": True},
        ]})
        assert res.status_code == 200

        # 上游收到的是别名：原名（精确）不再出现，defer_loading/占位工具被剥
        sent = b"".join(c[3] for c in mock.state.calls
                        if c[1].endswith("/messages") and c[3]).decode("utf-8", "ignore")
        assert "ToolSearchTool" in sent
        assert sent.count("ToolSearch") == sent.count("ToolSearchTool")  # 无裸原名
        assert "defer_loading" not in sent
        assert "DeferredToolPlaceholder" not in sent

        # 响应侧还原：客户端看到的一直是自己的工具名
        data = res.json()
        assert data["content"][0]["name"] == "ToolSearch"
        assert "ToolSearchTool" not in res.text

        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE


@pytest.mark.integration
class TestGateBusyHold:
    """忙碌断路器：持续 3008（重试仍拒）→ 先降速，抢在边缘封禁之前。"""

    async def test_sustained_3008_trips_process_wide_hold(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep
    ):
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_CONCURRENCY_WAIT", 0)
        monkeypatch.setattr(settings, "RETRY_429_TIMES", 1)
        seed_account(fresh_app, _JWT_BUSY, name="a-busy")
        mock.state.sequences[_JWT_BUSY[:16]] = ["concurrency_3008"]

        # 每个请求贡献一次「因并发耗尽放弃 Plan」；第 5 次（阈值）触发全进程 hold。
        # 触发请求本身由兜底按限流语义回 429（busy_hold），前 4 个为无号 503
        for i in range(5):
            res = await client.post("/v1/messages", json=_MSG_BODY)
            assert res.status_code == (429 if i == 4 else 503)
        assert gate.busy_remaining() > 0

        # hold 期间新请求本地拦截（限流语义 429，客户端按 Retry-After 退避），不打上游
        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 429
        assert res.json()["error"]["type"] == "gate_busy_hold"
        assert "Retry-After" in res.headers
        assert not _paths(mock, before)

    async def test_transient_3008_does_not_count(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep
    ):
        """一次重试就过去的 3008 是正常排队，不计饱和（magpie 0.1.55 教训）。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_CONCURRENCY_WAIT", 0)
        seed_account(fresh_app, _JWT_TR, name="a-tr")
        mock.state.sequences[_JWT_TR[:16]] = ["concurrency_3008", "ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        assert gate.busy_remaining() == 0  # 未触发任何 hold

    async def test_self_healed_3008_does_not_count(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep
    ):
        """连续两次 3008 后第 3 次成功 = 自愈，不得计入饱和（review P2-3）。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_CONCURRENCY_WAIT", 0)
        monkeypatch.setattr(settings, "RETRY_429_TIMES", 3)
        seed_account(fresh_app, _JWT_TR, name="a-selfheal")
        mock.state.sequences[_JWT_TR[:16]] = ["concurrency_3008", "concurrency_3008", "ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        assert gate.busy_remaining() == 0  # 请求最终成功 → 不算饱和

    async def test_gate_leak_on_client_disconnect(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """取消窗口（P2-2）：acquire 后连接被取消，闸门槽位必须归还（finally）。"""
        import asyncio

        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        seed_account(fresh_app, _JWT_TR, name="a-cancel")
        mock.state.sequences[_JWT_TR[:16]] = ["slow_first_byte"]  # 30s：留在连接/首字节

        task = asyncio.create_task(client.post("/v1/messages", json=_MSG_BODY))
        await asyncio.sleep(0.8)  # 已过 acquire、停在 await 首字节
        assert gate._inflight == 1  # 此刻持有闸门槽位
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # 让 finally 跑完
        assert gate._inflight == 0  # finally 归还，无泄漏


@pytest.mark.integration
class TestNoBalance1113:
    """1113「无余额」通道感知（2026-11-11 线上实证：死号每请求重探 151 次）。

    Plan 通道 = 套餐额度耗尽 → 标 EXHAUSTED；Key 通道 = 该 Key 无资源包 →
    选号冷却记忆（账号不标态）。判定走业务码精确解析，logid 含 "1113" 不撞。
    """

    async def _no_refresh(self, monkeypatch):
        from app.routes import gateway as gw

        async def _stub(account):
            return None

        monkeypatch.setattr(gw, "_safe_refresh", _stub)

    async def test_plan_1113_marks_exhausted_and_stops_reprobing(
        self, gateway_client, fresh_app, monkeypatch
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)  # 屏蔽后台 billing，计数只看 messages
        acc = seed_account(fresh_app, _JWT_1113_PLAN, name="a-1113-plan")
        mock.state.sequences[_JWT_1113_PLAN[:16]] = ["no_balance_1113"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 唯一账号耗尽
        assert len([x for x in _paths(mock, before)]) == 1  # 不原地重试

        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.EXHAUSTED  # 标态：选号不再重选重探
        assert "1113" in (after.last_error or "")

        # 第二个请求不再打上游（EXHAUSTED 不可选）
        before2 = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 503
        assert not _paths(mock, before2)  # 零重探（线上 151 次循环的根治点）

    async def test_key_1113_cools_selection_without_touching_status(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """Key 通道 1113：账号状态不改（Plan 可能仍在），但选号冷却停止重探；
        后台手动启用清除冷却、允许立即重探。"""
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        monkeypatch.setattr(settings, "KEY_NO_BALANCE_COOLDOWN", 600)
        acc = seed_account(fresh_app, _JWT_1113_KEY, name="a-1113-key")
        acc.api_key = "sk-key-no-balance-1113"
        acc.status = Status.DISABLED  # plan 死、仅 Key 可用（线上 briarlow17 形态）
        acc.risk_strikes = 5  # 3012 风控 5/5 禁用
        acc.last_error = "风控封禁 (3012/unusual activity) HTTP 405，确认恢复后请在后台手动启用（第 5 次）"
        fresh_app.update_account(acc)
        mock.state.sequences["sk-key-no-balance"[:16]] = ["no_balance_1113"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert _paths(mock, before) == [_KEY_PATH]  # 只探了 Key 通道一次
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.DISABLED  # 账号状态不被 Key-1113 污染
        assert after.risk_strikes >= 5  # 风控记录原样保留

        # 冷却记忆生效：第二个请求不再撞上游（151 次重探循环的根治点）
        before2 = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 503
        assert not _paths(mock, before2)

        # 后台手动启用 = 明确重试意图：清冷却（走 admin 端点，store 直调不清）
        res_en = await client.post(
            f"/admin/api/accounts/{acc.id}/enabled",
            json={"enabled": True}, headers=ADMIN_AUTH,
        )
        assert res_en.status_code == 200
        assert gw._key_no_balance.get(acc.id) is None
        # 启用后账号 ACTIVE：plan 通道恢复入梯，重探一次（仍 1113 → 标 EXHAUSTED）
        mock.state.sequences[_JWT_1113_KEY[:16]] = ["no_balance_1113"]
        before3 = len(mock.state.calls)
        res3 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res3.status_code == 503
        assert _paths(mock, before3) == [_PLAN_PATH]
        after3 = fresh_app.find("zai", acc.id)
        assert after3.status == Status.EXHAUSTED

    async def test_rate_limit_1302_with_1113_logid_is_not_no_balance(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """裸子串回归钉：1302 body 的 logid 含 "1113" 不得误入无余额分支
        （mock 场景 rate_limit_1302 的 logid 故意带 1113 子串）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RATE_LIMIT_COOLDOWN", 30)
        acc = seed_account(fresh_app, _JWT_1302, name="a-1302")
        mock.state.sequences[_JWT_1302[:16]] = ["rate_limit_1302"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 真限流：账号休息（非无余额路径）
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.COOLING  # 走了真限流分支
        assert after.status != Status.EXHAUSTED
        assert "1302" in (after.last_error or "")


@pytest.mark.integration
class TestKeyNoBalanceScope:
    """review 修复：冷却记忆不连坐 ACTIVE 双通道账号；凭证变更/删除即清。"""

    async def test_dual_channel_account_not_benched_by_key_1113(
        self, gateway_client, fresh_app, monkeypatch, no_retry_sleep
    ):
        """ACTIVE 双通道账号 Plan 失败 → Key 1113：冷却只记忆，**不跳过整号**，
        下一个请求仍走健康的 Plan 通道（review P2）。"""
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 0)  # Plan 一次 429 即下沉
        key = "sk-dual-no-balance"
        acc = seed_account(fresh_app, _JWT_MIX_KEY, name="a-dual")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[_JWT_MIX_KEY[:16]] = ["rate_limited"]      # Plan 通道
        mock.state.sequences[key[:16]] = ["no_balance_1113"]            # Key 通道

        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 503  # Plan 429 → Key 1113 → 换号（无其它账号）
        assert gw._key_no_balance.get(acc.id) is not None  # 记忆已记录

        # 第二个请求：不得被冷却跳过——Plan 通道仍被重试（有界浪费，非连坐）
        before = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 503
        paths = _paths(mock, before)
        assert _PLAN_PATH in paths  # Plan 通道再次被打到 = 未被连坐

    async def test_exhausted_branch_1113_on_key_does_not_mark_account(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """402/200 承载的 1113 在 Key 通道同样不标 EXHAUSTED（通道感知全覆盖）。"""
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _JWT_1113_KEY, name="a-1113-key-402")
        acc.api_key = "sk-key-no-balance-1113"
        acc.status = Status.DISABLED
        fresh_app.update_account(acc)
        mock.state.sequences["sk-key-no-balance"[:16]] = ["no_balance_1113_402"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.DISABLED  # 不被标 EXHAUSTED（Plan 可能仍在）
        assert gw._key_no_balance.get(acc.id) is not None

    async def test_credential_update_clears_cooldown(
        self, gateway_client, fresh_app
    ):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account
        import time as _time

        acc = seed_account(fresh_app, _JWT_MIX_KEY, name="a-edit-clear")
        gw._key_no_balance[acc.id] = _time.time() + 600

        res = await client.put(
            f"/admin/api/accounts/{acc.id}",
            json={"token": "hNew.eyJzdWIiOiJuIn0.sig"}, headers=ADMIN_AUTH,
        )
        assert res.status_code == 200
        assert gw._key_no_balance.get(acc.id) is None  # 凭证变更 = 立即重探

    async def test_delete_account_clears_cooldown(
        self, gateway_client, fresh_app
    ):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account
        import time as _time

        acc = seed_account(fresh_app, _JWT_MIX_KEY, name="a-del-clear")
        gw._key_no_balance[acc.id] = _time.time() + 600

        res = await client.request(
            "DELETE", "/admin/api/accounts", json=[acc.id], headers=ADMIN_AUTH,
        )
        assert res.status_code == 200
        assert gw._key_no_balance.get(acc.id) is None  # 删除即无残留


@pytest.mark.integration
class TestGateRefusalFailover:
    """闸门拒绝的调度语义（review P2-1）：hold 与账号无关，不得「连坐」有 Key 账号。"""

    async def test_mixed_pool_serves_via_key_during_edge_hold(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """A 无 Key（被 hold 拒）+ B 有 Key（可服务）：请求必须 200 而不是 503。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 30)
        seed_account(fresh_app, _JWT_MIX_PLAN, name="a-planonly")
        keyed = seed_account(fresh_app, _JWT_MIX_KEY, name="b-keyed")
        keyed.api_key = "sk-mixed-pool-key"
        fresh_app.update_account(keyed)
        gate.trip_edge()  # 直接处于封禁窗口内

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # B 的 Key 通道接住，不再被 A 连坐
        paths = _paths(mock, before)
        assert paths == [_KEY_PATH]  # 只打 Key 通道，plan 渠道零流量

    async def test_pure_plan_pool_gets_gate_reason_and_retry_after(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """纯 plan 池在 hold 期：兜底 503 带 gate 原因 + Retry-After（非「无可用账号」）。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 30)
        seed_account(fresh_app, _JWT_MIX_PLAN, name="a-planonly")
        gate.trip_edge()

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "gate_edge_hold"
        assert 1 <= int(res.headers["Retry-After"]) <= 30
        assert not _paths(mock, before)  # hold 期零上游流量
