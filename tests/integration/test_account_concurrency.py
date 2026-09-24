"""账号并发限制集成测试（2026-09-13）：单账号默认 2，可配置，0 = 不限。

依赖 mock 上游 slow 场景（0.3s 延迟）制造重叠；用 conc_max 仪表（按凭证）
断言单账号同时刻在飞请求数不超过限制。
"""

from __future__ import annotations

import asyncio

import pytest

from app import settings

_MSG_BODY = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}


def _seed(store, secret: str, name: str):
    return store.add_account("zai", name, secret)


def _per_key_max(mock, key: str) -> int:
    """某凭证观测到的最大同时刻在飞 messages 请求数。"""
    return (getattr(mock.state, "conc_max", {}) or {}).get(key, 0)


@pytest.mark.integration
class TestAccountConcurrency:
    @pytest.fixture(autouse=True)
    def _slow_upstream(self, gateway_client):
        """并发用例把默认场景替换为 slow（0.3s 延迟，制造重叠）；仪表逐用例清零。

        mock 上游是会话级实例：slow_all / conc_* 状态跨用例残留，进出各清一次。
        """
        _client, mock = gateway_client
        mock.state.slow_all = True
        mock.state.conc_in = {}
        mock.state.conc_max = {}
        yield
        mock.state.slow_all = False
        mock.state.conc_in = {}
        mock.state.conc_max = {}

    async def test_single_account_limits_to_two(self, gateway_client, fresh_app):
        """单账号 4 并发：恰 2 成功，其余 503；单号 max_concurrent ≤ 2。"""
        client, mock = gateway_client
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-cc")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = sorted(t.result().status_code for t in tasks)
        assert statuses == [200, 200, 503, 503]
        assert _per_key_max(mock, acc.jwt_token[:16]) <= 2

    async def test_two_accounts_all_succeed(self, gateway_client, fresh_app):
        """双账号 4 并发：每号 ≤2，全部成功。"""
        client, mock = gateway_client
        a = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-cc1")
        b = _seed(fresh_app, "hD.eyJzdWIiOiJkIn0.sig", "b-cc2")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200, 200]
        assert _per_key_max(mock, a.jwt_token[:16]) <= 2
        assert _per_key_max(mock, b.jwt_token[:16]) <= 2

    async def test_full_account_skips_to_next(self, gateway_client, fresh_app):
        """A 号满 2 → 调度跳到 B 号，第 3 个请求不 503。"""
        client, _mock = gateway_client
        _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-full")
        _seed(fresh_app, "hD.eyJzdWIiOiJkIn0.sig", "b-full")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(3)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200]

    async def test_zero_limit_unbounded(self, gateway_client, fresh_app):
        """limit=0：并发不封顶（4 请求同飞全 200，单号 max_concurrent > 2）。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "0")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-zero")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200, 200]
        assert _per_key_max(mock, acc.jwt_token[:16]) > 2

    async def test_runtime_setting_changes_limit(self, gateway_client, fresh_app):
        """后台改 limit=1 即生效：第 3 个并发请求被拒（单号 4 并发只成 1）。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-one")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(2)]
        statuses = sorted(t.result().status_code for t in tasks)
        assert statuses == [200, 503]
        assert _per_key_max(mock, acc.jwt_token[:16]) <= 1

    def test_env_default_is_two(self):
        assert settings.ACCOUNT_CONCURRENCY == 2

    async def test_429_wait_does_not_hold_concurrency_slot(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """429 等待不得占住并发槽：一号 429 睡着时，第二路请求仍能占用该号成功。"""
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        client, mock = gateway_client
        mock.state.slow_all = False
        fresh_app.set_setting("account_concurrency", "1")
        monkeypatch.setattr(settings, "RETRY_429_TIMES", 1)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 60)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)

        gate = asyncio.Event()
        entered = asyncio.Event()

        real_sleep = gw._sleep

        async def gated_sleep(seconds: float):
            if seconds and seconds > 0:
                entered.set()
                await gate.wait()
                return
            await real_sleep(seconds)

        monkeypatch.setattr(gw, "_sleep", gated_sleep)
        jwt = "hQ.eyJzdWIiOiJxIn0.sig"
        seed_account(fresh_app, jwt, name="a-slot")
        mock.state.sequences[jwt[:16]] = ["rate_limited", "ok"]

        first = asyncio.create_task(client.post("/v1/messages", json=_MSG_BODY))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await client.post("/v1/messages", json=_MSG_BODY)
        assert second.status_code == 200, second.text
        gate.set()
        first_res = await first
        assert first_res.status_code == 200

    async def test_skip_full_does_not_consume_account_attempts(
        self, gateway_client, fresh_app
    ):
        """满号跳过不得计入 MAX_ACCOUNT_ATTEMPTS。

        6 号 limit=1、前 5 号已占满时，round-robin 起点 2 会让当前实现
        连续抽到 5 个满号就 503，永远抽不到空闲的第 6 号（2026-09 review P2）。
        """
        from app.routes import gateway as gw

        client, mock = gateway_client
        mock.state.slow_all = False
        fresh_app.set_setting("account_concurrency", "1")
        accounts = [
            _seed(fresh_app, f"h{i}.eyJzdWIiOiJjIn0.sig", f"a-skip-{i}")
            for i in range(6)
        ]
        for acc in accounts[:5]:
            gw._inflight[acc.id] = 1
        fresh_app._rotation["zai"] = 2
        try:
            resp = await client.post("/v1/messages", json=_MSG_BODY)
            assert resp.status_code == 200, resp.text
        finally:
            for acc in accounts[:5]:
                gw._inflight.pop(acc.id, None)
