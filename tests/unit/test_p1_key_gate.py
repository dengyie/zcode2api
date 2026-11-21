"""P1-2 admin key 强度闸 + P3 public_view 掩码（2026-11-21 review 回归测试）。"""
from __future__ import annotations


def test_weak_default_key_generates_random(monkeypatch):
    """未设 env → 随机 token（≥12 字符），不再落 "zcode"。"""
    import importlib

    monkeypatch.delenv("ZCODE_ADMIN_KEY", raising=False)
    from app import settings

    mod = importlib.reload(settings)
    assert len(mod.DEFAULT_ADMIN_KEY) >= 12
    assert mod.DEFAULT_ADMIN_KEY != "zcode"


def test_weak_env_key_rejected_and_replaced(monkeypatch, capsys):
    """env 显式弱 key 被拒绝采用并说明，生成随机值兜底。"""
    import importlib

    monkeypatch.setenv("ZCODE_ADMIN_KEY", "short")
    from app import settings

    mod = importlib.reload(settings)
    assert len(mod.DEFAULT_ADMIN_KEY) >= 12
    out = capsys.readouterr().out
    assert "强度不足" in out
    # 恢复
    monkeypatch.delenv("ZCODE_ADMIN_KEY")
    importlib.reload(settings)


def test_strong_env_key_adopted(monkeypatch, capsys):
    monkeypatch.setenv("ZCODE_ADMIN_KEY", "a-very-strong-admin-key-123")
    import importlib

    from app import settings

    mod = importlib.reload(settings)
    assert mod.DEFAULT_ADMIN_KEY == "a-very-strong-admin-key-123"
    monkeypatch.delenv("ZCODE_ADMIN_KEY")
    importlib.reload(settings)


def test_public_view_masks_short_secrets():
    from app.models import Account

    short = Account(id="1", name="n", provider="zai", mode="apiKey", api_key="sk-123")
    assert short.public_view()["token_masked"] == "••••••"
    mid = Account(id="2", name="n", provider="zai", mode="apiKey", api_key="sk-1234567890ab")
    masked = mid.public_view()["token_masked"]
    assert "sk-1234567890ab" not in masked
    long = Account(id="3", name="n", provider="zai", mode="jwt",
                   jwt_token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig-value-long")
    lm = long.public_view()["token_masked"]
    assert lm.startswith("eyJhbGci") and "…" in lm


def test_schema_evolution_preserves_unknown_fields():
    """降级兼容（2026-11-21 review P3）：新版 blob 的未知字段在旧版加载后
    原样回写，update_account 落库不再永久抹掉新字段。"""
    from app.models import Account

    acc = Account.from_dict({"id": "x", "name": "t", "provider": "zai",
                             "mode": "jwt", "jwt_token": "h.p.s",
                             "future_field": "KEEP-ME"})
    assert acc._extra["future_field"] == "KEEP-ME"
    persisted = acc.to_persist_dict()
    assert persisted["future_field"] == "KEEP-ME"
    assert persisted["id"] == "x"
    # 已知字段照常
    acc2 = Account.from_dict({"id": "y", "name": "t", "provider": "zai",
                              "mode": "jwt", "jwt_token": "h.p.s"})
    assert not hasattr(acc2, "_extra")
    assert acc2.to_persist_dict() == acc2.to_dict()


def test_cooling_without_until_not_stuck_forever():
    """COOLING+None 防御（2026-11-21 review 待确认项）：现行写入方全部成对
    设置 cooling_until，但若未来路径漏配，原先该号永久不可选且无自愈——
    现在按「冷却到期」处理，宁可放回池子也不蒸发。"""
    from app.models import Account, Status

    acc = Account(id="1", name="n", provider="zai", mode="jwt",
                  jwt_token="h.p.s", status=Status.COOLING, cooling_until=None)
    assert acc.is_selectable() is True
    assert acc.is_cooling() is False
    # 正常冷却仍受 cooling_until 约束
    import time as _t
    acc2 = Account(id="2", name="n", provider="zai", mode="jwt",
                   jwt_token="h.p.s", status=Status.COOLING,
                   cooling_until=_t.time() + 100)
    assert acc2.is_selectable() is False
    assert acc2.is_cooling() is True
