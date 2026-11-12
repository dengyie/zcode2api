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
        import time as _time

        from app.routes import gateway as gw
        from tests.conftest import seed_account

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
        import time as _time

        from app.routes import gateway as gw
        from tests.conftest import seed_account

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


@pytest.mark.integration
class TestPoolWindowGate:
    """套餐窗口闸门（pool-hold，2026-11-12）：真空期快速失败，不白撞 5 轮账号。

    判定口径 = models.Account.next_window_start 的保守语义：
      - 全池「确认枯竭」（有余额但窗口全过期 + 过期后实查 quota_verified_at >
        max(expires)，或余额归零）且无未来窗口 → 拦 429 + Retry-After；
      - 任意一号快照空/陈旧（不可判定）→ 放行（宁可白撞一次也不错拦）；
      - billing 刷新发现可用窗口 → 自动解除。
    """

    # mock 的 sequences 按凭证前 16 位累计，每个用例独立凭证
    _JWT_PW1 = "hP1w.eyJzdWIiOiJ4In0.sig"
    _JWT_PW2 = "hP2w.eyJzdWIiOiJ4In0.sig"
    _JWT_PW3 = "hP3w.eyJzdWIiOiJ4In0.sig"

    async def _no_refresh(self, monkeypatch):
        from app.routes import gateway as gw

        async def _stub(account):
            return None

        monkeypatch.setattr(gw, "_safe_refresh", _stub)

    def _dead_snapshot(self, acc, *, verified_offset: float = -1800.0,
                       future_plans: bool = False, now: float | None = None) -> None:
        """把账号置成「确认枯竭」形态：窗口过期 + 余额有量 + 过期后实查证据。"""
        now = now or time.time()
        acc.quota = {"GLM-5.3": {"total": 100, "used": 100, "remaining": 5,
                                 "expires_at": now - 3600}}
        acc.quota_verified_at = now + verified_offset  # 默认过期后 30min 实查
        if future_plans:
            acc.plans = [{"entitlements": [
                {"period": "daily", "effective_at": now + 3600, "ends_at": now + 90000}]}]
        else:
            acc.plans = []

    async def test_confirmed_exhaustion_blocks_with_retry_after(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """全池确认枯竭 → 触发 hold，第二个请求入口快速 429 + Retry-After，零上游流量。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_PW1, name="a-pw1")
        self._dead_snapshot(acc)
        fresh_app.update_account(acc)

        # 第一请求：EXHAUSTED 号不可选 → 无号 503；exhaust 分支顺带扫池触发 hold
        before = len(mock.state.calls)
        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 503
        assert not _paths(mock, before)
        assert gate.pool_hold_remaining() > 0  # 闸门已被 exhaust 分支触发

        # 第二请求：入口快速失败，gate_pool_hold 语义
        before2 = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 429
        body2 = res2.json()["error"]
        assert body2["type"] == "gate_pool_hold"
        assert "Retry-After" in res2.headers
        assert not _paths(mock, before2)  # 0 次上游调用

        # 解除路径 1：hold 到期自然失效——用一个独立 Gate 验证「到期即失效」
        # 语义（公共 API：note 一个已过去的窗口点 + 上限 1s，hold 立即耗尽），
        # 不触碰私有字段
        from app.gate import Gate as _Gate

        expired = _Gate()
        expired.note_pool_exhausted(time.time() + 3600)  # 先置正常 hold
        monkeypatch.setattr(settings, "GATE_POOL_HOLD_MAX_SECONDS", 1)
        expired.clear_pool_hold()  # 公共清除入口
        expired.note_pool_exhausted(time.time() - 5, now=time.time() - 6)  # 过去窗口点 → cap=1s
        # max 语义对全新实例无历史 hold 可保留：hold=1s 后自然过期
        expired._pool_hold_until -= 2  # 唯一可控的时间平移：模拟 1s 流逝
        assert expired.pool_hold_remaining() <= 0
        assert expired.refusal_reason() is None
        # 原闸门（本用例持有的 gate 单例）此刻仍处 hold——用 clear 公共入口解除
        gate.clear_pool_hold()
        assert gate.refusal_reason() is None

    async def test_future_window_retry_after_points_to_it(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """plans[] 带未来 effective_at：Retry-After 指向窗口点（cap 内）。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_PW2, name="a-pw2")
        self._dead_snapshot(acc, future_plans=True)  # 未来 1h 有新窗口
        fresh_app.update_account(acc)

        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 503
        assert gate.pool_hold_remaining() > 3000  # hold 至未来窗口点（~3600s）

        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 429
        wait = int(res2.headers["Retry-After"])
        assert 3500 <= wait <= 3600  # 指向下一个已知生效点

    async def test_stale_snapshot_does_not_block(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """快照陈旧（quota_verified_at < expires）= 不可判定 → 绝不拦（保守优先）。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_PW3, name="a-pw3")
        now = time.time()
        # 有余额但窗口过期，实查时间**早于**过期点 → 无资格判死
        acc.quota = {"GLM-5.3": {"total": 100, "used": 100, "remaining": 5,
                                 "expires_at": now - 3600}}
        acc.quota_verified_at = now - 7200
        acc.status = Status.EXHAUSTED  # gateway 已把它标死（本请求真实撞过 1113）
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert gate.pool_hold_remaining() == 0  # 闸门未触发：陈旧快照不放证据

    async def test_quota_recovery_clears_hold(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """hold 期间 billing 刷新发现可用窗口 → 自动解除，后续请求放行。"""
        client, mock = gateway_client
        from app.gate import gate
        from app.quota import fetch_quota
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_PW1, name="a-pw-recover")
        self._dead_snapshot(acc, future_plans=True)
        fresh_app.update_account(acc)
        gate.note_pool_exhausted(time.time() + 3600)  # 模拟已触发 hold

        # billing 刷新回「有量未过期」→ fetch_quota 恢复分支清 hold
        mock.state.sequences[self._JWT_PW1[:16]] = ["ok"]
        result = await fetch_quota(acc)
        assert "error" not in result
        assert gate.pool_hold_remaining() == 0
        assert gate.refusal_reason() is None
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE  # 顺带确认状态机恢复

    async def test_apikey_fallback_pool_not_blocked(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """hold 生效但池内有 Key 回退（风控禁用形态）：入口预检不拦，Key 通道照常服务。"""
        client, mock = gateway_client
        from app.gate import gate
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_PW2, name="a-pw-keyed")
        acc.api_key = "sk-pw-fallback-key"
        acc.status = Status.DISABLED  # plan 死（briarlow17 形态），仅 Key 可用
        fresh_app.update_account(acc)
        gate.note_pool_exhausted(time.time() + 3600)  # 模拟 plan 渠道已处 hold
        mock.state.sequences["sk-pw-fallback-key"[:16]] = ["ok"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # 入口预检放行（有 Key），Key 通道接住
        assert _paths(mock, before) == [_KEY_PATH]  # plan 零流量（被 hold 拦在 _try_account）

    async def test_mixed_pool_with_dead_key_gets_pool_hold_429(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """review P2 #1 钉子：混合池里唯一有 Key 号的 Key 也已 1113 冷却——
        入口预检必须拦（429 gate_pool_hold），不能放行进循环落裸 503。"""
        client, mock = gateway_client
        import time as _time

        from app.gate import gate
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        # A：无 Key，确认枯竭（EXHAUSTED，select 滤光）
        acc_a = seed_account(fresh_app, self._JWT_PW1, name="a-pw-dead")
        self._dead_snapshot(acc_a)
        fresh_app.update_account(acc_a)
        # B：DISABLED + 持 Key（briarlow17 形态），Key 已进 1113 冷却
        acc_b = seed_account(fresh_app, self._JWT_PW2, name="b-pw-deadkey")
        self._dead_snapshot(acc_b)  # 快照也确认枯竭，否则判定保守放行
        acc_b.api_key = "sk-pw-dead-key"
        acc_b.status = Status.DISABLED
        fresh_app.update_account(acc_b)
        gw._key_no_balance[acc_b.id] = _time.time() + 600

        # 首请求：A/B 全不可选 → 无号兜底 503，顺带触发 hold
        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 503
        assert gate.pool_hold_remaining() > 0

        # 第二请求：Key 也死 → 预检不再放行（修复前会落裸 503）
        before = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 429
        assert res2.json()["error"]["type"] == "gate_pool_hold"
        assert "Retry-After" in res2.headers
        assert not _paths(mock, before)  # 零上游流量

    async def test_future_window_with_dead_ends_at_is_skipped(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """review #4 钉子：entitlement 起点在未来但 ends_at 早于起点（脏数据）
        → 不作为窗口点，hold 用 cap 兜底而非等到死窗口。"""
        from app.models import Account as _Account

        acc = _Account.create("zai", "t-dirty", "hP9z.eyJzdWIiOiJ4In0.sig")
        now = time.time()
        acc.quota = {"GLM-5.3": {"total": 100, "used": 100, "remaining": 0,
                                 "expires_at": now - 3600}}
        acc.quota_verified_at = now - 1800
        acc.plans = [{"entitlements": [
            {"period": "daily", "effective_at": now + 7200, "ends_at": now + 3600},  # 死窗口
            {"period": "daily", "effective_at": now + 9000},  # 健康窗口
        ]}]
        v = acc.next_window_start()
        assert v is not None and abs(v - (now + 9000)) < 1, v  # 跳过死窗口取健康的


@pytest.mark.integration
class TestPlanModelExhaust:
    """模型级额度耗尽冷却（plan_guard，2.7.29）：（账号，模型）粒度跳号。

    线上实证：GLM-5.3 旗舰上游 1005 exceed quota limit 时同号 Flash 窗口仍有
    2.9 亿——点名单一模型的请求不该把全池每号都撞一遍必然失败的 1005。
    """

    # mock 的 sequences 按凭证前 16 位累计，每个用例独立凭证
    _JWT_ME1 = "hMe1.eyJzdWIiOiJ4In0.sig"
    _JWT_ME2 = "hMe2.eyJzdWIiOiJ4In0.sig"
    _JWT_ME3 = "hMe3.eyJzdWIiOiJ4In0.sig"
    _JWT_ME4 = "hMe4.eyJzdWIiOiJ4In0.sig"
    _JWT_ME5 = "hMe5.eyJzdWIiOiJ4In0.sig"

    async def _no_refresh(self, monkeypatch):
        from app.routes import gateway as gw

        async def _stub(account):
            return None

        monkeypatch.setattr(gw, "_safe_refresh", _stub)

    async def test_flagship_1005_marks_cooling_next_request_skips(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """A 号旗舰 1005 → plan_guard 记（A, GLM-5.3）；账号被 billing 刷新救活
        （ACTIVE）但旗舰仍冷却时，第二个同模型请求跳过 A 不再撞 1005。"""
        client, mock = gateway_client
        from app import plan_guard
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_ME1, name="a-me1")
        mock.state.sequences[self._JWT_ME1[:16]] = ["quota_exhausted_200"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 单号池，耗尽后无号
        assert plan_guard.cooling(acc.id, "GLM-5.3")  # 模型级记忆已打点

        # billing 刷新把号救回 ACTIVE（窗口恢复的号），但旗舰 1005 冷却仍在 TTL 内
        acc.status = Status.ACTIVE
        acc.quota = {"GLM-5.3-Flash": {"remaining": 100, "expires_at": time.time() + 3600}}
        acc.quota_verified_at = time.time()
        fresh_app.update_account(acc)

        # 同模型再请求：A 被模型级冷却跳过（日志可见「额度耗尽冷却中，跳过」）→ 503
        before = len(mock.state.calls)
        res1 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res1.status_code == 503
        assert len(_paths(mock, before)) == 0  # A 零上游流量——没撞第二次 1005

        # Flash（异模型）请求：不连坐，A 被正常选中并成功
        mock.state.sequences[self._JWT_ME1[:16]] = ["ok"]
        before2 = len(mock.state.calls)
        res2 = await client.post("/v1/messages",
                                 json={**_MSG_BODY, "model": "GLM-5.3-Flash"})
        assert res2.status_code == 200
        assert len(_paths(mock, before2)) == 1  # Flash 走到了 A 的 Plan 通道

    async def test_other_model_not_contaminated(self, gateway_client, fresh_app, monkeypatch):
        """A 号旗舰 1005 冷却中（账号已被救活 ACTIVE），点名 Flash（异模型）不连坐。"""
        client, mock = gateway_client
        from app import plan_guard
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_ME3, name="a-me3")
        mock.state.sequences[self._JWT_ME3[:16]] = ["quota_exhausted_200", "ok"]

        res1 = await client.post("/v1/messages", json=_MSG_BODY)  # 旗舰撞 1005
        assert res1.status_code == 503
        assert plan_guard.cooling(acc.id, "GLM-5.3")

        # billing 刷新救回 ACTIVE（Flash 窗口在账），旗舰冷却仍在
        acc.status = Status.ACTIVE
        acc.quota = {"GLM-5.3-Flash": {"remaining": 100, "expires_at": time.time() + 3600}}
        acc.quota_verified_at = time.time()
        fresh_app.update_account(acc)

        res2 = await client.post("/v1/messages",
                                 json={**_MSG_BODY, "model": "GLM-5.3-Flash"})
        assert res2.status_code == 200  # Flash 不受旗舰冷却影响，A 被选中
        assert not plan_guard.cooling(acc.id, "GLM-5.3-Flash")

    async def test_billing_recovery_clears_cooling(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """quota.fetch_quota 实查到可用窗口 → 定向清除该模型的冷却（2.7.30）。

        接线测试：走真实 fetch_quota（不 stub _safe_refresh），mock 默认 balance
        返回 GLM-5.3 可用窗口 → 断言（A, GLM-5.3）冷却被清。"""
        client, mock = gateway_client
        from app import plan_guard, quota
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)  # messages 路径不刷，fetch_quota 由本用例直调
        acc = seed_account(fresh_app, self._JWT_ME2, name="a-me2")
        plan_guard.note(acc.id, "GLM-5.3", time.time() + 3600)
        assert plan_guard.cooling(acc.id, "GLM-5.3")

        await quota.fetch_quota(acc)  # mock 默认 GLM-5.3 remaining=2e6 未过期

        assert not plan_guard.cooling(acc.id, "GLM-5.3")  # billing 事实推翻冷却

    async def test_billing_recovery_does_not_clear_other_model(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """定向清除不连坐（review P2 根因）：billing 账本里不存在的模型，冷却
        原样保留——billing 只能证明「在账且可用」的模型恢复了，证明不了其它
        模型没耗尽。否则旗舰冷却会被 Flash 窗口反复洗掉，1005 重探从
        1 次/30min 退化成 1 次/60s（billing 去抖间隔）。"""
        client, mock = gateway_client
        from app import plan_guard, quota
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_ME4, name="a-me4")
        plan_guard.note(acc.id, "GLM-5.3", time.time() + 3600)        # mock 默认账本在账且可用
        plan_guard.note(acc.id, "GLM-5.3-Flash", time.time() + 3600)  # 不在 mock 账本

        await quota.fetch_quota(acc)

        assert not plan_guard.cooling(acc.id, "GLM-5.3")        # 在账且可用 → 清
        assert plan_guard.cooling(acc.id, "GLM-5.3-Flash")      # 不在账 → 冷却保留

    async def test_429_1113_plan_marks_cooling(self, gateway_client, fresh_app, monkeypatch):
        """429+1113（Plan 通道）打模型级冷却：与 1005 同口径（gateway.py:1381）。"""
        client, mock = gateway_client
        from app import plan_guard
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, self._JWT_ME5, name="a-me5")
        mock.state.sequences[self._JWT_ME5[:16]] = ["no_balance_1113"]  # 429+1113

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert plan_guard.cooling(acc.id, "GLM-5.3")  # Plan 分支同步打点

        # 账号被救活（ACTIVE）后同模型请求不再撞 1113
        acc.status = Status.ACTIVE
        acc.quota = {"GLM-5.3-Flash": {"remaining": 100, "expires_at": time.time() + 3600}}
        acc.quota_verified_at = time.time()
        fresh_app.update_account(acc)
        before = len(mock.state.calls)
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 503
        assert not _paths(mock, before)  # 冷却跳号，零上游流量

    async def test_key_1113_does_not_touch_plan_guard(self, gateway_client, fresh_app, monkeypatch):
        """Key 通道 1113 不打模型级冷却（channel 守卫）：Key 无资源包 ≠ Plan
        模型额度耗尽，误打点会把 Plan 通道健康模型连坐 30 分钟。"""
        client, mock = gateway_client
        from app import plan_guard
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        await self._no_refresh(monkeypatch)
        acc = seed_account(fresh_app, _JWT_1113_KEY, name="a-key1113-pg")
        acc.api_key = "sk-key1113-pg-test"
        acc.status = Status.DISABLED  # 仅 Key 可用
        fresh_app.update_account(acc)
        mock.state.sequences["sk-key1113-pg-te"[:16]] = ["no_balance_1113"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert gw._key_no_balance.get(acc.id) is not None  # Key 冷却记忆有
        assert not plan_guard.cooling(acc.id, "GLM-5.3")   # 模型级冷却不误打

    async def test_ttl_expiry_reprobes(self, monkeypatch):
        """TTL 到期自动摘除（自然重探），不等 billing。"""
        from app import plan_guard

        plan_guard.note("acct-y", "GLM-5.3", time.time() - 1)  # 已过期
        assert not plan_guard.cooling("acct-y", "GLM-5.3")
