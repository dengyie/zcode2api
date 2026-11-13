"""跨 provider 降级（Magpie 模式）集成测试。

裸模型名默认路由 zai，zai 池空时自动下沉 bigmodel（按 PROVIDER_FALLBACK_ORDER）。
`bigmodel/` 前缀只在 bigmodel 池内调度（chain 末尾，不跨回 zai）。
"""

from __future__ import annotations

import pytest

_ZAI_JWT = "xpf1.eyJzdWIiOiJ6MX0.sig"
_BM_KEY = "xpf2-bigmodel-api-key"


def _messages_calls(mock) -> list:
    return [c for c in mock.state.calls
            if c[0] == "POST" and c[1].endswith("/anthropic/v1/messages")]


def _setup(mock):
    """每个测试前复位 mock state。"""
    mock.state.calls.clear()
    mock.state.counters.clear()
    mock.state.sequences.clear()


@pytest.mark.integration
class TestCrossProviderFallback:
    async def test_bare_model_zai_pool_empty_falls_to_bigmodel(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """裸模型名：zai 池无可用号 → 下沉 bigmodel → 成功。"""
        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)

        # 只加 bigmodel 账号，不加 zai
        bm = fresh_app.add_account("bigmodel", "bm-fallback", _BM_KEY)

        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        assert res.status_code == 200
        assert res.json()["type"] == "message"
        calls = _messages_calls(mock)
        assert len(calls) == 1, f"got {len(calls)} calls: {[(c[1],) for c in calls]}"
        # 走 bigmodel 的 /api/anthropic/v1/messages
        assert calls[0][1] == "/api/anthropic/v1/messages"
        assert bm.use_count == 1

    async def test_zai_prefix_falls_to_bigmodel(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """`zai/` 前缀也是从 zai 进入 chain → 下沉 bigmodel。"""
        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)

        fresh_app.add_account("bigmodel", "bm-fb2", _BM_KEY + "2")

        res = await client.post("/v1/messages", json={
            "model": "zai/GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        assert res.status_code == 200
        assert res.json()["type"] == "message"

    async def test_both_pools_have_accounts_zai_selected_first(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """两池都有账号：裸模型名优先从 zai 池选。"""
        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)

        zai_acc = fresh_app.add_account("zai", "zai-first",
                                         "xpf3.eyJzdWIiOiJ6YXkifQ.sig")
        bm_acc = fresh_app.add_account("bigmodel", "bm-second", _BM_KEY + "3")

        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        assert res.status_code == 200
        # zai 账号被优先使用
        assert zai_acc.use_count == 1
        assert bm_acc.use_count == 0

    async def test_bigmodel_prefix_only_bigmodel_pool(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """`bigmodel/` 前缀只在 bigmodel 池调度。"""
        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)

        bm = fresh_app.add_account("bigmodel", "bm-explicit", _BM_KEY + "4")

        res = await client.post("/v1/messages", json={
            "model": "bigmodel/GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        assert res.status_code == 200
        assert res.json()["type"] == "message"
        calls = _messages_calls(mock)
        assert len(calls) == 1, f"got {len(calls)} calls"
        # bigmodel 的 /api/anthropic/v1/messages
        assert calls[0][1] == "/api/anthropic/v1/messages"
        assert bm.use_count == 1

    async def test_bigmodel_pool_empty_no_zai_fallback(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """`bigmodel/` 前缀 + bigmodel 池空 → 503（不跨回 zai）。"""
        client, _ = gateway_client
        stub_captcha.needs_captcha = False

        # 只加 zai 账号，不加 bigmodel
        fresh_app.add_account("zai", "zai-only", "xpf4.eyJzdWIiOiJ6bHkifQ.sig")

        res = await client.post("/v1/messages", json={
            "model": "bigmodel/GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        # Bigmodel pool empty, no fallback back to zai (bigmodel 已在 chain 末尾)
        assert res.status_code == 503

    async def test_attempt_budget_is_per_provider(
        self, gateway_client, fresh_app, stub_captcha, monkeypatch
    ):
        """尝试预算是按 provider 计的（2.7.33）。

        旧实现 `attempts` 在 provider 循环外共享：zai 池里第一个号失败就烧掉整份
        预算（MAX_ACCOUNT_ATTEMPTS），bigmodel 一次也轮不到，降级链名存实亡。
        这里把预算压到 1，zai 号 401 失败后 bigmodel 必须仍然被尝试。
        """
        from app.routes import gateway as gw

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)
        monkeypatch.setattr(gw, "MAX_ACCOUNT_ATTEMPTS", 1)

        zai_acc = fresh_app.add_account("zai", "zai-budget",
                                        "xpf5.eyJzdWIiOiJ6NWYifQ.sig")
        bm_acc = fresh_app.add_account("bigmodel", "bm-budget", "xpf6-bigmodel-api-key")
        mock.state.sequences[zai_acc.jwt_token[:16]] = ["auth_invalid"]

        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })

        assert res.status_code == 200, res.text
        calls = _messages_calls(mock)
        assert any(c[1] == "/api/anthropic/v1/messages" for c in calls), calls
        assert bm_acc.use_count == 1
        assert zai_acc.use_count == 0

    async def test_cross_provider_affinity_does_not_leak(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """跨 provider 降级后会话亲和只绑定最终成功的 provider 账号。"""
        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        _setup(mock)

        # zai 池空，bigmodel 有号
        bm = fresh_app.add_account("bigmodel", "bm-affinity", _BM_KEY + "5")

        # 第一轮请求，裸模型名 → zai 池空 → 下沉 bigmodel → 成功
        res1 = await client.post("/v1/messages", json={
            "model": "GLM-5.3",
            "messages": [{"role": "user", "content": "first turn"}],
        })
        assert res1.status_code == 200
        assert bm.use_count == 1

        # 第二轮（多轮会话）：同一 context 继续用 bigmodel 账号
        res2 = await client.post("/v1/messages", json={
            "model": "GLM-5.3",
            "messages": [
                {"role": "user", "content": "first turn"},
                {"role": "assistant", "content": "response"},
                {"role": "user", "content": "second turn"},
            ],
        })
        assert res2.status_code == 200
        # 复用同一账号（会话亲和）
        assert bm.use_count >= 1
