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
    """三路 billing 全回 200+1005：必须报 error（不是「成功但没有数据」），并标 EXHAUSTED。

    旧逻辑三路都只是 `data.get("data") or {}` → 函数返回空 dict → 后台当成刷新
    成功、账号状态不变，额度耗尽永远发现不了。
    """
    _client, mock = gateway_client
    acc = seed_account(fresh_app, _JWT_ALL_ERR, name="a-allerr")
    _bind(mock, _JWT_ALL_ERR, "billing_all_quota_200")

    res = await quota.fetch_quota(acc)

    assert "error" in res and "1005" in res["error"]
    after = fresh_app.list_accounts("zai")[0]
    assert after.status == Status.EXHAUSTED
    assert after.quota == {}  # 不得写入残缺快照
