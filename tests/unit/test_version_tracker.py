"""version_tracker 模块单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app import constants
from app.version_tracker import (
    VersionInfo,
    VersionTracker,
    is_newer_version,
    parse_manifest,
    parse_semver,
)


def test_parse_semver():
    assert parse_semver("3.17.2") == (3, 17, 2)
    assert parse_semver("v3.18.0-beta.1") == (3, 18, 0, 1)
    assert parse_semver("") == (0,)
    assert parse_semver("invalid") == (0,)


def test_is_newer_version():
    assert is_newer_version("3.17.2", "3.17.1") is True
    assert is_newer_version("3.18.0", "3.17.9") is True
    assert is_newer_version("4.0.0", "3.99.99") is True
    assert is_newer_version("3.17.1", "3.17.1") is False
    assert is_newer_version("3.17.0", "3.17.1") is False
    assert is_newer_version("3.16.9", "3.17.0") is False


def test_parse_manifest_zh_locale():
    yaml_text = """version: 3.17.2
releaseDate: "2026-11-02T06:43:49.471Z"
releaseName: Release v3.17.2
releaseNotes: |-
    General Notes
releaseNotesByLocale:
    zh-CN:
        title: "中文更新"
        markdown: |-
            ## 新功能
            - 官方测试新功能
    en-US:
        markdown: |-
            ## New Features
            - English features
"""
    info = parse_manifest(yaml_text)
    assert info is not None
    assert info.version == "3.17.2"
    assert info.release_date == "2026-11-02T06:43:49.471Z"
    assert info.release_name == "Release v3.17.2"
    assert "官方测试新功能" in info.release_notes


def test_parse_manifest_fallback_notes():
    yaml_text = """version: 3.18.0
releaseDate: "2026-11-10T12:00:00.000Z"
releaseNotes: |-
    Fallback Markdown Notes
"""
    info = parse_manifest(yaml_text)
    assert info is not None
    assert info.version == "3.18.0"
    assert info.release_notes == "Fallback Markdown Notes"


def test_parse_manifest_malformed():
    assert parse_manifest("") is None
    assert parse_manifest("   ") is None
    assert parse_manifest("key: [unclosed") is None
    assert parse_manifest("some_other_key: 123") is None


def test_parse_manifest_version_float_not_truncated():
    """YAML 1.1 陷阱：`version: 3.10` 被 safe_load 解析成 float 3.1，str() 后
    版本号截断为 "3.1"，语义比较 (3,1) < (3,17,1) 恒判旧——必须回退原始字面量。"""
    info = parse_manifest('version: 3.10\nreleaseDate: "2026-11-10"\n')
    assert info is not None
    assert info.version == "3.10"

    # 带引号字面量不受影响（str 路径）
    info2 = parse_manifest('version: "3.10"\n')
    assert info2 is not None
    assert info2.version == "3.10"


@pytest.mark.asyncio
async def test_tracker_freshness_cache(fresh_app, monkeypatch):
    """新鲜窗口内复用上次结果（不再打上游）；force=True 绕过缓存强制核验。"""
    tracker = VersionTracker()
    mock_info = VersionInfo(
        version="3.17.1", release_date="", release_name="", release_notes="", checked_at=0.0
    )
    fetch = AsyncMock(return_value=mock_info)
    monkeypatch.setattr(tracker, "fetch_remote_manifest", fetch)

    res1 = await tracker.check_and_sync()
    res2 = await tracker.check_and_sync()
    assert fetch.await_count == 1
    assert res2 is res1

    res3 = await tracker.check_and_sync(force=True)
    assert fetch.await_count == 2
    assert res3 is not res1


@pytest.mark.asyncio
async def test_tracker_concurrent_checks_dedup(fresh_app, monkeypatch):
    """并发触发（领取轮前置核验 + 后台轮 + 手动核验重叠）只打一次上游。"""
    import asyncio

    tracker = VersionTracker()
    mock_info = VersionInfo(
        version="3.17.1", release_date="", release_name="", release_notes="", checked_at=0.0
    )
    fetch = AsyncMock(return_value=mock_info)
    monkeypatch.setattr(tracker, "fetch_remote_manifest", fetch)

    r1, r2 = await asyncio.gather(tracker.check_and_sync(), tracker.check_and_sync())
    assert fetch.await_count == 1
    assert r1["ok"] is True
    assert r2 is r1


@pytest.mark.asyncio
async def test_tracker_check_and_sync_auto_sync(fresh_app, monkeypatch):
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    try:
        mock_info = VersionInfo(
            version="3.18.5",
            release_date="2026-11-05",
            release_name="Release v3.18.5",
            release_notes="Notes 3.18.5",
            checked_at=123456.0,
        )
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "true")

        res = await tracker.check_and_sync()
        assert res["ok"] is True
        assert res["has_update"] is True
        assert res["updated"] is True
        assert res["latest_version"] == "3.18.5"
        assert constants.CLIENT_APP_VERSION == "3.18.5"
        assert fresh_app.get_setting("zcode_latest_version") == "3.18.5"
    finally:
        constants.set_active_version(orig)


@pytest.mark.asyncio
async def test_tracker_check_and_sync_manual_only(fresh_app, monkeypatch):
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    try:
        mock_info = VersionInfo(
            version="3.18.9",
            release_date="2026-11-06",
            release_name="Release v3.18.9",
            release_notes="Notes",
            checked_at=123456.0,
        )
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "false")

        res = await tracker.check_and_sync()
        assert res["ok"] is True
        assert res["has_update"] is True
        assert res["updated"] is False
        assert res["latest_version"] == "3.18.9"
        # 伪装版本未被自动修改
        assert constants.CLIENT_APP_VERSION == orig
    finally:
        constants.set_active_version(orig)


def test_tracker_init_from_store(fresh_app):
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    try:
        fresh_app.set_setting("auto_sync_client_version", "true")
        fresh_app.set_setting("zcode_latest_version", "3.19.0")
        fresh_app.set_setting(
            "zcode_latest_info",
            json.dumps({"version": "3.19.0", "release_name": "v3.19.0"}),
        )
        tracker.init_from_store()
        assert constants.CLIENT_APP_VERSION == "3.19.0"
        assert tracker.latest_info is not None
        assert tracker.latest_info.version == "3.19.0"
    finally:
        constants.set_active_version(orig)
