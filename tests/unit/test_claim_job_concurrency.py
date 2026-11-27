"""手动 POST /admin/api/claim 后台 job 并发（2.7.83）。

原 _run_claim_job 逐号串行：N 号 × 单号耗时（billing 重试 + 换绑链）线性累加。
改为 Semaphore(CLAIM_ROUND_CONCURRENCY) 并发后：
- 在飞数恰好封顶于配置值（不超、也确实打满）
- 单号异常只落该号 outcome，不中断其它号、不让 gather 抛出
- 全部跑完 rec["done"] 置位，每号恰一条 outcome
- 缺省并发 = 10
"""

from __future__ import annotations

import asyncio

import pytest

from app import settings
from app.claim import ClaimError
from app.routes import admin_api


class _Acc:
    def __init__(self, i: int):
        self.id = f"acc-{i}"
        self.name = f"acct-{i}"


def test_default_concurrency_is_ten():
    assert settings.CLAIM_ROUND_CONCURRENCY == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 3, 10])
async def test_manual_claim_job_concurrency_capped(monkeypatch, cap):
    monkeypatch.setattr(settings, "CLAIM_ROUND_CONCURRENCY", cap)
    inflight = {"n": 0, "max": 0}

    async def fake_do_claim(acc, plan_id):
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await asyncio.sleep(0.05)
        inflight["n"] -= 1
        return {"plan_name": "P"}

    async def fake_refresh(_accs):
        return None

    monkeypatch.setattr(admin_api, "do_claim", fake_do_claim)
    monkeypatch.setattr(admin_api, "refresh_accounts", fake_refresh)

    accounts = [_Acc(i) for i in range(12)]
    rec: dict = {"outcomes": [], "done": False}
    await admin_api._run_claim_job_guarded(accounts, None, rec)

    assert rec["done"] is True
    assert inflight["max"] == min(cap, len(accounts))
    assert sorted(o["account_id"] for o in rec["outcomes"]) == sorted(a.id for a in accounts)
    assert all(o["ok"] for o in rec["outcomes"])


@pytest.mark.asyncio
async def test_manual_claim_job_isolates_per_account_failures(monkeypatch):
    monkeypatch.setattr(settings, "CLAIM_ROUND_CONCURRENCY", 10)

    async def fake_do_claim(acc, plan_id):
        await asyncio.sleep(0.01)
        if acc.id == "acc-1":
            raise ClaimError("没有待领取的套餐")
        if acc.id == "acc-2":
            raise ValueError("boom")
        return {"plan_name": "P"}

    async def fake_refresh(_accs):
        return None

    monkeypatch.setattr(admin_api, "do_claim", fake_do_claim)
    monkeypatch.setattr(admin_api, "refresh_accounts", fake_refresh)

    accounts = [_Acc(i) for i in range(5)]
    rec: dict = {"outcomes": [], "done": False}
    await admin_api._run_claim_job_guarded(accounts, None, rec)

    by_id = {o["account_id"]: o for o in rec["outcomes"]}
    assert len(rec["outcomes"]) == 5
    assert by_id["acc-1"]["ok"] is False and "待领取" in by_id["acc-1"]["message"]
    assert by_id["acc-2"]["ok"] is False and "boom" in by_id["acc-2"]["message"]
    assert all(by_id[f"acc-{i}"]["ok"] for i in (0, 3, 4))
