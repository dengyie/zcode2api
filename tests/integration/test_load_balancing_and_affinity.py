"""多账号负载均衡与会话亲和性集成测试（v2.7.21）。

验证：
1. 新请求多账号负载均衡（交替轮换分流至不同账号）。
2. 同一上下文会话亲和性（显式 Header 与多轮消息指纹均稳定命中同账号）。
3. 满 2 溢出与硬上限约束（亲和账号满 2/2 时优雅溢出至其他账号，单号在飞恒 ≤ 2）。
4. 亲和账号故障自动转移（亲和号禁用或冷却时自动切健康号并更新绑定）。
"""

from __future__ import annotations

import pytest

from app.affinity import affinity_manager
from app.models import Status
from app.routes import gateway as gw


def _seed(store, secret: str, name: str):
    return store.add_account("zai", name, secret)


def _get_call_auths(mock) -> list[str]:
    """提取 mock 上游接收到的各次 messages 请求的 JWT 凭证片段。"""
    auths = []
    for _method, path, headers, _body in getattr(mock.state, "calls", []):
        if "/messages" in path:
            hdr_dict = dict(headers)
            auth = (
                hdr_dict.get("authorization", "").removeprefix("Bearer ")
                or hdr_dict.get("x-api-key", "")
            )
            auths.append(auth[:16])
    return auths


@pytest.mark.integration
class TestLoadBalancingAndAffinity:
    @pytest.fixture(autouse=True)
    def _clear_affinity(self, gateway_client):
        affinity_manager.clear()
        _client, mock = gateway_client
        mock.state.calls = []
        mock.state.conc_in = {}
        mock.state.conc_max = {}
        mock.state.sequences = {}
        yield
        affinity_manager.clear()
        mock.state.calls = []
        mock.state.conc_in = {}
        mock.state.conc_max = {}
        mock.state.sequences = {}

    async def test_new_requests_load_balance_across_accounts(self, gateway_client, fresh_app):
        """新请求均匀分流：连续 4 个独立请求在双账号间交替轮换。"""
        client, mock = gateway_client
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # 4 个独立的单轮新请求
        for i in range(4):
            resp = await client.post(
                "/v1/messages",
                json={"model": "GLM-5.2", "messages": [{"role": "user", "content": f"query {i}"}]},
            )
            assert resp.status_code == 200

        auths = _get_call_auths(mock)
        assert len(auths) == 4
        # 验证负载均衡交替分配到 a 和 b
        assert auths[0] == a.jwt_token[:16]
        assert auths[1] == b.jwt_token[:16]
        assert auths[2] == a.jwt_token[:16]
        assert auths[3] == b.jwt_token[:16]

    async def test_session_affinity_with_explicit_header(self, gateway_client, fresh_app):
        """显式 Header（x-session-id）维持会话粘性：同 session 请求始终路由到同一账号。"""
        client, mock = gateway_client
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # 会话 S1：第 1 轮（进入负载均衡，分到 a）
        r1 = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-s1"},
            json={"model": "GLM-5.2", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert r1.status_code == 200

        # 新会话 S2：第 1 轮（新请求，分到 b）
        r2 = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-s2"},
            json={"model": "GLM-5.2", "messages": [{"role": "user", "content": "world"}]},
        )
        assert r2.status_code == 200

        # 会话 S1：第 2 轮（继续命中 a）
        r3 = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-s1"},
            json={
                "model": "GLM-5.2",
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                    {"role": "user", "content": "how are you?"},
                ],
            },
        )
        assert r3.status_code == 200

        # 会话 S2：第 2 轮（继续命中 b）
        r4 = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-s2"},
            json={
                "model": "GLM-5.2",
                "messages": [
                    {"role": "user", "content": "world"},
                    {"role": "assistant", "content": "earth"},
                    {"role": "user", "content": "what else?"},
                ],
            },
        )
        assert r4.status_code == 200

        auths = _get_call_auths(mock)
        assert auths == [
            a.jwt_token[:16],  # S1-1 -> a
            b.jwt_token[:16],  # S2-1 -> b
            a.jwt_token[:16],  # S1-2 -> a (sticky)
            b.jwt_token[:16],  # S2-2 -> b (sticky)
        ]

    async def test_session_affinity_with_multi_turn_fingerprint(self, gateway_client, fresh_app):
        """无 Header 客户端：凭借多轮对话上下文特征指纹粘性路由到同一账号。"""
        client, mock = gateway_client
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        _b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # 第 1 轮（首轮文本 "Explain quantum physics"）
        r1 = await client.post(
            "/v1/messages",
            json={
                "model": "GLM-5.2",
                "messages": [{"role": "user", "content": "Explain quantum physics"}],
            },
        )
        assert r1.status_code == 200

        # 第 2 轮（携带 assistant 往返历史，包含首轮有效用户文本）
        r2 = await client.post(
            "/v1/messages",
            json={
                "model": "GLM-5.2",
                "messages": [
                    {"role": "user", "content": "Explain quantum physics"},
                    {"role": "assistant", "content": "Quantum mechanics describes nature at atomic scales."},
                    {"role": "user", "content": "What about superposition?"},
                ],
            },
        )
        assert r2.status_code == 200

        auths = _get_call_auths(mock)
        assert len(auths) == 2
        # 两轮严格命中同一账号 a
        assert auths[0] == a.jwt_token[:16]
        assert auths[1] == a.jwt_token[:16]

    async def test_pinned_account_saturated_spillover_to_next(self, gateway_client, fresh_app):
        """亲和账号满载 2/2 时自动溢出至空闲账号，且单账号在飞绝不超 2。"""
        client, mock = gateway_client
        mock.state.slow_all = True  # 慢响应制造在飞并发
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # 预先将 S1 绑定到 a
        affinity_manager.bind("zai:hdr:session-s1", a.id)

        # 模拟 a 当前在飞为 2（已达上限）
        gw._inflight[a.id] = 2

        try:
            # 发起 S1 的请求，由于 a 满载，自动溢出到 b 且不报 503
            resp = await client.post(
                "/v1/messages",
                headers={"x-session-id": "session-s1"},
                json={"model": "GLM-5.2", "messages": [{"role": "user", "content": "spillover test"}]},
            )
            assert resp.status_code == 200
        finally:
            gw._inflight.pop(a.id, None)

        auths = _get_call_auths(mock)
        assert len(auths) == 1
        # 请求成功落到 b
        assert auths[0] == b.jwt_token[:16]

    async def test_pinned_account_cooling_fails_over(self, gateway_client, fresh_app):
        """亲和账号进入冷却期时自动故障转移至健康账号并更新绑定。"""
        client, mock = gateway_client
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # S1 绑定到 a
        affinity_manager.bind("zai:hdr:session-s1", a.id)

        # a 进入冷却
        a.status = Status.COOLING
        fresh_app.update_account(a)

        # S1 发起请求，自动切换至 b
        resp = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-s1"},
            json={"model": "GLM-5.2", "messages": [{"role": "user", "content": "cooling failover"}]},
        )
        assert resp.status_code == 200

        auths = _get_call_auths(mock)
        assert auths[-1] == b.jwt_token[:16]
        # 亲和缓存已自动更新绑定为 b
        assert affinity_manager.get("zai:hdr:session-s1") == b.id

    async def test_error_response_does_not_bind_affinity(self, gateway_client, fresh_app):
        """上游错误（如 404）直接透传回客户端，不建立亲和绑定，防止污染后续正常调度。"""
        client, mock = gateway_client
        _a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")
        _b = _seed(fresh_app, "hB.eyJzdWIiOiJiIn0.sig", "acc-b")

        # 触发上游 404 not_found
        mock.state.sequences[_a.jwt_token[:16]] = ["not_found"]
        resp = await client.post(
            "/v1/messages",
            headers={"x-session-id": "session-err"},
            json={"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404
        # 验证亲和性管理器中无绑定记录
        assert affinity_manager.get("zai:hdr:session-err") is None

    async def test_async_concurrency_race_does_not_exceed_cap(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """验证在并发突发且存在异步挂起点时，单账号在飞并发严格 <= 2，消除 Check-then-Act 竞态。"""
        import asyncio

        from app.routes import gateway as gw

        client, mock = gateway_client
        mock.state.slow_all = True
        a = _seed(fresh_app, "hA.eyJzdWIiOiJhIn0.sig", "acc-a")

        # 在 model_requests_need_captcha 注入异步调度让步（引发竞态时序）
        orig_need_captcha = gw.captcha_manager.model_requests_need_captcha

        async def _delayed_captcha(*args, **kwargs):
            await asyncio.sleep(0.02)
            return await orig_need_captcha(*args, **kwargs)

        monkeypatch.setattr(gw.captcha_manager, "model_requests_need_captcha", _delayed_captcha)

        # 突发并发 4 个请求打单账号
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    client.post(
                        "/v1/messages",
                        json={"model": "GLM-5.2", "messages": [{"role": "user", "content": f"race {i}"}]},
                    )
                )
                for i in range(4)
            ]

        statuses = sorted(t.result().status_code for t in tasks)
        # 恰有 2 个成功，其余 503
        assert statuses == [200, 200, 503, 503]
        # mock 记录到的最大在飞请求数严格 <= 2
        assert mock.state.conc_max.get(a.jwt_token[:16], 0) <= 2

