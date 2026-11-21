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
