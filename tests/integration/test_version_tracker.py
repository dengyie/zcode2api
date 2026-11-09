"""版本探测与 Admin API 集成测试。"""

from __future__ import annotations

import pytest

from app import constants


@pytest.mark.asyncio
async def test_check_zcode_version_now(gateway_client, monkeypatch):
    client, mock = gateway_client
    mock.state.mock_manifest_version = "3.18.0"
    orig = constants.CLIENT_APP_VERSION
    # 模拟提示词已移植到 3.18.0（正文锁同步抬高）——只有此时才允许自动对齐
    monkeypatch.setattr(constants, "PROMPT_ALIGNED_VERSION", "3.18.0")
    try:
        res = await client.post("/admin/api/version/zcode/check", headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["latest_version"] == "3.18.0"
        # 默认 auto_sync 为 true，自动对齐至 3.18.0
        assert data["updated"] is True
        assert data["prompt_port_required"] is False
        assert constants.CLIENT_APP_VERSION == "3.18.0"
    finally:
        constants.set_active_version(orig)


@pytest.mark.asyncio
async def test_check_zcode_version_refuses_unported(gateway_client):
    """官方新版高于提示词对齐版本：记录更新但不顶版本头。

    头新于 system 正文会触发上游 405/3012，后台以 prompt_port_required
    提示「需移植 prompt.mjs」。
    """
    client, mock = gateway_client
    mock.state.mock_manifest_version = "3.18.0"
    orig = constants.CLIENT_APP_VERSION
    try:
        res = await client.post("/admin/api/version/zcode/check", headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["latest_version"] == "3.18.0"
        assert data["updated"] is False
        assert data["prompt_port_required"] is True
        assert data["prompt_aligned_version"] == constants.PROMPT_ALIGNED_VERSION
        assert constants.CLIENT_APP_VERSION == orig
    finally:
        constants.set_active_version(orig)


@pytest.mark.asyncio
async def test_settings_includes_version_info(gateway_client):
    client, _mock = gateway_client
    res = await client.get("/admin/api/settings", headers={"Authorization": "Bearer zcode"})
    assert res.status_code == 200
    data = res.json()
    assert "auto_sync_client_version" in data
    assert "zcode_client_version" in data
    assert "zcode_latest_version" in data
    assert data["auto_sync_client_version"] is True


@pytest.mark.asyncio
async def test_update_settings_toggle_auto_sync(gateway_client):
    client, _mock = gateway_client
    res = await client.put(
        "/admin/api/settings",
        json={"auto_sync_client_version": False},
        headers={"Authorization": "Bearer zcode"},
    )
    assert res.status_code == 200

    # 查验变动
    res2 = await client.get("/admin/api/settings", headers={"Authorization": "Bearer zcode"})
    assert res2.status_code == 200
    assert res2.json()["auto_sync_client_version"] is False
