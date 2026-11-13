"""fetch_quota 的额度过期判定（2026-10-31 线上实证）。

mock 的 billing/balance 默认恒返回 `remaining=2000000` + 2099 过期；这里给账号
绑定 `balance_expired` 场景（只剩余额、窗口早已过期），验证这类号会被判
EXHAUSTED，不再当作可路由账号。选场景靠凭证前缀（`_bind_key`），billing 请求
没有 x-mock-bind 头也能命中。
"""

from __future__ import annotations

import time

from app import quota
from app.models import Status
from tests.conftest import seed_account

# mock_server 是 session 级、sequences 按凭证前 16 位做 key —— 三个用例必须用
# **不同** JWT，否则前一个用例绑的 balance_expired 会污染后一个。
_JWT_EXPIRED = "expire01.payload.signature"
_JWT_LIVE = "live0001.payload.signature"
_JWT_RENEW = "renew001.payload.signature"


def _bind(mock, jwt: str, scenario: str) -> None:
    mock.state.sequences[jwt[:16]] = [scenario]


async def test_expired_windows_mark_exhausted(gateway_client, fresh_app):
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_EXPIRED, name="a-expired")
    _bind(mock, _JWT_EXPIRED, "balance_expired")

    res = await quota.fetch_quota(acc)

    assert "error" not in res
    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.EXHAUSTED
    assert after.last_error == "额度已过期"
    # 快照照常落库（前端要能看到「已过期但还有 X 余额」）
    assert after.quota["GLM-5.3"]["remaining"] == 2_000_000
    # 判死必须带「过期后成功查过」的证据（滚动日窗：没查过的过期快照不封号）
    assert after.quota_verified_at and after.quota_verified_at > time.time() - 60
    assert after.is_selectable() is False


async def test_live_window_keeps_account_active(gateway_client, fresh_app):
    """对照组：未过期时行为不变（mock 默认 2099 过期）。"""
    acc = seed_account(fresh_app, _JWT_LIVE, name="a-live")
    await quota.fetch_quota(acc)
    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.ACTIVE
    assert after.is_selectable() is True


async def test_expired_then_renewed_becomes_active_again(gateway_client, fresh_app):
    """窗口续期后再次刷新 → 从 EXHAUSTED 恢复 ACTIVE（过期不是永久禁用）。"""
    acc = seed_account(fresh_app, _JWT_RENEW, name="a-renew")
    acc.quota = {"GLM-5.3": {"remaining": 5, "expires_at": time.time() - 10}}
    acc.status = Status.EXHAUSTED
    acc.last_error = "额度已过期"
    fresh_app.update_account(acc)

    _client, _mock = gateway_client  # mock 给的是 2099 过期 + 余额
    await quota.fetch_quota(acc)
    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.ACTIVE
    assert after.last_error is None


_JWT_BILLING = "billing01.payload.signature"


async def test_billing_200_with_quota_code_does_not_wipe_snapshot(gateway_client, fresh_app):
    """billing/current 回 HTTP 200 + {"code":1005} 时：不得把 plans 洗空、不得当成功。

    这是 200 盲区在 billing 侧的形态：旧逻辑 `plans = data.data.plans or []`
    直接覆盖，结果「上游额度拒绝」被写成「上游没有投放套餐」。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_BILLING, name="a-billing-200")
    acc.plans = [{"plan_id": "keep-me"}]
    acc.plan = {"plan_id": "keep-me"}
    fresh_app.update_account(acc)
    _bind(mock, _JWT_BILLING, "billing_quota_200")

    res = await quota.fetch_quota(acc)

    after = fresh_app.list_accounts("zai")[0]
    # balance 正常返回（mock 默认有余额）→ 不算全失败；但 plans 不能被错误覆盖
    assert after.plans == [{"plan_id": "keep-me"}]
    assert after.plan == {"plan_id": "keep-me"}
    assert res.get("billing", {}).get("code") == 1005  # 原始响应保留给后台排查


_JWT_ALL_ERR = "allerr01.payload.signature"


async def test_all_billing_business_errors_reported_as_failure(gateway_client, fresh_app):
    """三路 billing 全回 200+1005：必须报 error（不是「成功但没有数据」）。

    但**不得据此把号判死**——上游对新号的 billing 支持并不一致（历史实证：空
    billing 的号仍 100% 成功服务请求），判死会让这类号永远没机会被真实请求验证。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_ALL_ERR, name="a-allerr")
    _bind(mock, _JWT_ALL_ERR, "billing_all_quota_200")

    res = await quota.fetch_quota(acc)

    assert "error" in res and "1005" in res["error"]
    after = fresh_app.list_accounts("zai")[0]
    assert after.quota == {}  # 不得写入残缺快照
    assert after.status == Status.ACTIVE  # 但也不得判死：留给真实请求定夺
    assert after.is_selectable() is True


_JWT_EMPTY = "empty001.payload.signature"


async def test_new_account_with_empty_billing_still_selectable(gateway_client, fresh_app):
    """新号形态：billing 三路 200 但全空（体验套餐不在账面表达）。

    旧逻辑：空响应被当「刷新成功但没数据」，账号静默不可用且没有线索。
    现逻辑：保持可选，`last_error` 明确写出「待首次请求验证」，后台能一眼看出
    这个号不是坏了、而是还没被上游计费系统登记。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_EMPTY, name="a-empty")
    _bind(mock, _JWT_EMPTY, "billing_empty")

    res = await quota.fetch_quota(acc)
    assert "error" not in res

    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.ACTIVE
    assert after.is_selectable() is True
    assert after.quota == {}
    assert "待首次请求验证" in (after.last_error or "")


_JWT_COOLING = "cooling1.payload.signature"
_JWT_BONUS = "bonus001.payload.signature"
_JWT_PLANS = "plans001.payload.signature"


async def test_cooling_not_downgraded_to_exhausted(gateway_client, fresh_app):
    """冷却期内的号刷新到「无可用窗口」时保留 COOLING（2.7.33）。

    EXHAUSTED 的 is_selectable 恒 False（不看 cooling_until）——降级等于把还在
    指数退避的号永久踢出池子；保留 COOLING 让冷却到期后自然重探。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_COOLING, name="a-cooling")
    acc.status = Status.COOLING
    acc.cooling_until = time.time() + 600
    acc.last_error = "命中风控，指数退避冷却"
    fresh_app.update_account(acc)
    _bind(mock, _JWT_COOLING, "balance_expired")

    await quota.fetch_quota(acc)

    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.COOLING
    assert after.is_cooling() is True
    assert "冷却" in (after.last_error or "")


async def test_bonus_plan_revives_exhausted_account_without_balance_windows(
    gateway_client, fresh_app
):
    """balance 空但 plans 有生效的一次性赠送池 → EXHAUSTED 复活（2.7.33）。

    原实现只在 quota_map 非空时计算 has_bonus，空 balances 的号永远无法复活。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_BONUS, name="a-bonus")
    now = time.time()
    acc.status = Status.EXHAUSTED
    acc.last_error = "额度已用完"
    acc.plans = [{"plan_id": "gift", "entitlements": [
        {"period": "one_time", "effective_at": now - 60, "ends_at": now + 3600},
    ]}]
    acc.plan = acc.plans[0]
    fresh_app.update_account(acc)
    _bind(mock, _JWT_BONUS, "billing_empty")

    await quota.fetch_quota(acc)

    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.ACTIVE
    assert after.last_error is None
    assert after.is_selectable() is True


async def test_empty_billing_plans_do_not_wipe_local_plan_cache(gateway_client, fresh_app):
    """billing/current 返回空 plans 时不得洗掉已知套餐（2.7.33）。"""
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_PLANS, name="a-plans")
    acc.plans = [{"plan_id": "keep-me"}]
    acc.plan = {"plan_id": "keep-me"}
    fresh_app.update_account(acc)
    _bind(mock, _JWT_PLANS, "billing_empty")

    await quota.fetch_quota(acc)

    after = fresh_app.list_accounts("zai")[0]
    assert after.plans == [{"plan_id": "keep-me"}]
    assert after.plan == {"plan_id": "keep-me"}



_JWT_TRIAL = "trial001.payload.signature"


async def test_new_account_trial_served_end_to_end(gateway_client, fresh_app):
    """新号端到端：billing 全空 → 仍被选中 → 真实请求成功 → 状态确认可用。

    这是「新号必须有体验套餐」这条产品事实的系统级回归：只用 billing 账本判断
    会把新号直接判死，而它其实第一个请求就能服务。同时验证成功后「待首次请求
    验证」提示被清掉（不然后台会一直挂着一条过期告示）。
    """
    client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_TRIAL, name="a-trial")
    _bind(mock, _JWT_TRIAL, "billing_empty")

    await quota.fetch_quota(acc)
    assert fresh_app.list_accounts("zai")[0].last_error

    res = await client.post("/v1/messages", json={
        "model": "glm-5.3", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert res.status_code == 200
    assert res.json()["content"][0]["text"] == "Hello from mock upstream"

    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.ACTIVE
    assert after.use_count == 1
    assert after.last_error is None


_JWT_TRIAL_DEAD = "dead0001.payload.signature"


async def test_new_account_without_quota_fails_once_then_exhausted(gateway_client, fresh_app):
    """反面对照：新号其实没额度 → 一次真实请求后判 EXHAUSTED，不再反复试错。

    「billing 空就放行」必须配这条闸，否则新号会无限撞 1005（每圈一次真实上游
    调用），而不会自己停下来。
    """
    client, mock = gateway_client
    seed_account(fresh_app, _JWT_TRIAL_DEAD, name="a-trial-dead")
    mock.state.sequences[_JWT_TRIAL_DEAD[:16]] = ["quota_exhausted_200"]
    # billing 走同一凭证前缀，同样落到 quota_exhausted_200 之外的分支即可
    # （billing_* 场景只认 balance_expired/billing_*，quota_exhausted_200 → 默认成功）

    res = await client.post("/v1/messages", json={
        "model": "glm-5.3", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert res.status_code >= 400  # 网关不再把 1005 当 200 透传

    after = fresh_app.list_accounts("zai")[0]
    assert after.use_count == 0
    assert after.status == Status.EXHAUSTED
    assert after.is_selectable() is False


async def test_empty_balance_does_not_verify_expiry(gateway_client, fresh_app):
    """billing 200 但 `balances` 为空（新号 billing 全空形态）≠「过期已确认」。

    2026-11-03 review：quota_verified_at 曾在 balance 业务码为 None 时就打点，
    空响应会把旧的过期快照误升格为「过期后实查确认」，把 2.7.8 要救的号从
    另一扇门判死。现在打点绑定在 quota_map 真实写库上。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, "emptybal01.payload.signature", name="a-empty")
    # 预置「窗口已过期的旧快照」（模拟日窗翻新后未刷新的本地状态）
    acc.quota = {"GLM-5.3": {"remaining": 100, "expires_at": time.time() - 600}}
    mock.state.sequences[acc.jwt_token[:16]] = ["billing_empty"]

    res = await quota.fetch_quota(acc)

    assert "error" not in res
    after = fresh_app.list_accounts("zai")[0]
    assert after.quota_verified_at is None          # 空 balances 没确认任何东西
    assert after.status == Status.ACTIVE            # 未知 → 不判死
    assert after.is_selectable() is True            # 放行让上游定生死


async def test_real_windows_still_stamp_verified_at(gateway_client, fresh_app):
    """对照：balances 非空时打点照常（「过期后实查」证据链不被破坏）。"""
    _client, mock = gateway_client
    acc = seed_account(fresh_app, "emptybal02.payload.signature", name="a-verified")
    acc.quota = {"GLM-5.3": {"remaining": 100, "expires_at": time.time() - 600}}
    mock.state.sequences[acc.jwt_token[:16]] = ["balance_expired"]

    await quota.fetch_quota(acc)

    after = fresh_app.list_accounts("zai")[0]
    assert after.quota_verified_at is not None and after.quota_verified_at > time.time() - 60
    assert after.status == Status.EXHAUSTED         # 有余额但过期，且过期后实查过 → 判死
    assert after.is_selectable() is False
