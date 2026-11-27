"""version_tracker 模块单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app import constants
from app.constants import is_newer_version, parse_semver
from app.version_tracker import (
    VersionInfo,
    VersionTracker,
    parse_manifest,
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
        # 模拟「提示词已移植到 4.1.0」：正文锁同步抬高后自动对齐才被允许
        monkeypatch.setattr(constants, "PROMPT_ALIGNED_VERSION", "4.1.0")
        mock_info = VersionInfo(
            version="4.1.0",
            release_date="2026-11-28",
            release_name="Release v4.1.0",
            release_notes="Notes 4.1.0",
            checked_at=123456.0,
        )
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "true")

        res = await tracker.check_and_sync()
        assert res["ok"] is True
        assert res["has_update"] is True
        assert res["updated"] is True
        assert res["messages_updated"] is True
        assert res["prompt_port_required"] is False
        assert res["latest_version"] == "4.1.0"
        assert constants.CLIENT_APP_VERSION == "4.1.0"
        assert fresh_app.get_setting("zcode_latest_version") == "4.1.0"
    finally:
        constants.set_active_version(orig)


@pytest.mark.asyncio
async def test_auto_sync_refuses_unported_newer_version(fresh_app, monkeypatch):
    """正文锁：官方新版高于提示词对齐版本时只记录、不顶 messages 版本头。

    头新于 system 正文 = 405/3012，宁可滞后也不自动错版；后台用
    prompt_port_required 标记「需移植 prompt.mjs」。
    billing 链无正文锁（2026-11-21 定案：claim 有效性优先），仍独立跟随。
    """
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    billing_orig = constants.BILLING_APP_VERSION
    try:
        mock_info = VersionInfo(
            version="4.1.0", release_date="2026-11-28", release_name="v4.1.0"
        )
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "true")

        res = await tracker.check_and_sync()
        assert res["has_update"] is True
        # updated=True 来自 billing 跟随；messages 头未被顶（正文锁拒绝）
        assert res["billing_updated"] is True
        assert res["messages_updated"] is False
        assert res["prompt_port_required"] is True
        assert res["prompt_aligned_version"] == constants.PROMPT_ALIGNED_VERSION
        assert constants.CLIENT_APP_VERSION == orig
        # billing 版本被顶上去（无正文锁，claim 有效性优先）
        assert constants.BILLING_APP_VERSION == "4.1.0"
        # 新版本仍落库（后台展示更新日志用）
        assert fresh_app.get_setting("zcode_latest_version") == "4.1.0"
    finally:
        constants.set_active_version(orig)
        constants.BILLING_APP_VERSION = billing_orig


@pytest.mark.asyncio
async def test_tracker_check_and_sync_manual_only(fresh_app, monkeypatch):
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    try:
        mock_info = VersionInfo(
            version="4.1.0",
            release_date="2026-11-28",
            release_name="Release v4.1.0",
            release_notes="Notes",
            checked_at=123456.0,
        )
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "false")

        res = await tracker.check_and_sync()
        assert res["ok"] is True
        assert res["has_update"] is True
        # auto_sync=false 只挡 messages 链；billing 链无条件跟随（2026-11-21 定案）
        assert res["updated"] is True
        assert res["billing_updated"] is True
        assert res["latest_version"] == "4.1.0"
        # messages 伪装版本未被自动修改，billing 已跟随
        assert constants.CLIENT_APP_VERSION == orig
        assert constants.BILLING_APP_VERSION == "4.1.0"
    finally:
        constants.set_active_version(orig)


def test_tracker_init_from_store_respects_prompt_lock(fresh_app):
    """缓存里比提示词新的版本不能直接顶上来（正文锁同样约束启动恢复）。

    billing 链不受正文锁约束：init 恢复阶段即跟随缓存最新版（2026-11-21 定案）。
    """
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    billing_orig = constants.BILLING_APP_VERSION
    try:
        fresh_app.set_setting("auto_sync_client_version", "true")
        fresh_app.set_setting("zcode_latest_version", "3.19.0")
        fresh_app.set_setting(
            "zcode_latest_info",
            json.dumps({"version": "3.19.0", "release_name": "v3.19.0"}),
        )
        tracker.init_from_store()
        assert constants.CLIENT_APP_VERSION == orig
        # billing 无正文锁：重启即跟随缓存最新版，不等下一轮 manifest 轮询
        assert constants.BILLING_APP_VERSION == "3.19.0"
        # 最新版本信息仍恢复（后台展示更新日志）
        assert tracker.latest_info is not None
        assert tracker.latest_info.version == "3.19.0"
    finally:
        constants.set_active_version(orig)
        constants.BILLING_APP_VERSION = billing_orig


def test_tracker_init_from_store_applies_aligned_version(fresh_app):
    """缓存版本在正文锁以内时正常恢复。"""
    tracker = VersionTracker()
    orig = constants.CLIENT_APP_VERSION
    try:
        constants.set_active_version("3.16.0")
        fresh_app.set_setting("auto_sync_client_version", "true")
        fresh_app.set_setting("zcode_latest_version", "3.17.1")
        tracker.init_from_store()
        assert constants.CLIENT_APP_VERSION == "3.17.1"
    finally:
        constants.set_active_version(orig)


@pytest.mark.asyncio
async def test_billing_follow_skipped_when_pinned_by_env(fresh_app, monkeypatch):
    """kill-switch：ZCODE_BILLING_APP_VERSION env 钉值时 billing 不自动跟随。"""
    tracker = VersionTracker()
    billing_orig = constants.BILLING_APP_VERSION
    try:
        monkeypatch.setattr(constants, "PROMPT_ALIGNED_VERSION", "4.1.0")
        monkeypatch.setenv("ZCODE_BILLING_APP_VERSION", "3.18.2")
        mock_info = VersionInfo(version="4.1.0", release_name="v4.1.0")
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "true")

        res = await tracker.check_and_sync()
        assert res["billing_followable"] is False
        assert res["billing_updated"] is False
        assert res["updated"] is True  # messages 已移植，正常对齐
        assert constants.BILLING_APP_VERSION == "3.18.2"
    finally:
        constants.BILLING_APP_VERSION = billing_orig


@pytest.mark.asyncio
async def test_billing_follow_skips_prerelease(fresh_app, monkeypatch):
    """预发布版（beta/rc）不得穿透：parse_semver 纯数字提取会把
    '3.19.0-beta.1' 解析成 (3,19,0,1) > (3,19,0)，billing 是唯一无正文锁
    链路，manifest 推 beta 时必须挡住。"""
    tracker = VersionTracker()
    billing_orig = constants.BILLING_APP_VERSION
    try:
        mock_info = VersionInfo(version="3.19.0-beta.1", release_name="v3.19.0-beta.1")
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        fresh_app.set_setting("auto_sync_client_version", "false")

        res = await tracker.check_and_sync()
        assert res["billing_followable"] is False
        assert res["billing_updated"] is False
        assert res["updated"] is False
        assert constants.BILLING_APP_VERSION == billing_orig
    finally:
        constants.BILLING_APP_VERSION = billing_orig


# ── billing 跟版实弹探针（2026-11-21 P1）─────────────────────────────────────


def _add_billing_account(fresh_app, name: str, status=None):
    """造一个可打 billing 的 JWT 号入池（active）。

    token 必须是 JWT 形态（header.payload.sig）——Account.create 按 secret
    形态分流 mode，裸字符串会归成 apiKey（allows_billing=False）。"""

    acc = fresh_app.add_account("zai", name, "mock-header.mock-payload.sig")
    if status is not None and acc.status != status:
        acc.status = status
        fresh_app.update_account(acc)
    return acc


@pytest.mark.asyncio
async def test_probe_passes_when_preview_ok(fresh_app, monkeypatch):
    """preview 全链成功 → 探针通过 → billing 版本保持抬升、不回滚。"""
    from app import version_tracker as vt

    fresh_app.set_setting("auto_sync_client_version", "false")
    _add_billing_account(fresh_app, "probe-ok")
    monkeypatch.setattr(
        vt, "_billing_probe_ok", AsyncMock(return_value=True)
    )
    tracker = vt.VersionTracker()
    billing_orig = constants.BILLING_APP_VERSION
    try:
        mock_info = VersionInfo(version="3.19.0", release_name="v3.19.0")
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        res = await tracker.check_and_sync()
        assert res["billing_updated"] is True
        assert constants.BILLING_APP_VERSION == "3.19.0"
    finally:
        constants.BILLING_APP_VERSION = billing_orig


@pytest.mark.asyncio
async def test_probe_rollback_on_upstream_reject(fresh_app, monkeypatch):
    """探针上游拒绝（ClaimError 非瞬态）→ 回滚基线 + billing_updated 归零。

    kill-switch 的人工止血自动化：坏版本不会静默撑到领取全灭。"""
    from app import version_tracker as vt

    fresh_app.set_setting("auto_sync_client_version", "false")
    _add_billing_account(fresh_app, "probe-reject")
    monkeypatch.setattr(
        vt, "_billing_probe_ok", AsyncMock(return_value=False)
    )
    tracker = vt.VersionTracker()
    billing_orig = constants.BILLING_APP_VERSION
    try:
        mock_info = VersionInfo(version="3.19.0", release_name="v3.19.0")
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        res = await tracker.check_and_sync()
        assert res["billing_updated"] is False
        assert constants.BILLING_APP_VERSION == billing_orig
    finally:
        constants.BILLING_APP_VERSION = billing_orig


@pytest.mark.asyncio
async def test_probe_transient_network_error_does_not_rollback(fresh_app, monkeypatch):
    """探针网络瞬态（ConnectError/超时）不算上游拒绝——不回滚，避免出口
    抖动误杀跟版。"""
    from app import version_tracker as vt
    from app.claim import ClaimError

    fresh_app.set_setting("auto_sync_client_version", "false")
    _add_billing_account(fresh_app, "probe-transient")
    tracker = vt.VersionTracker()
    billing_orig = constants.BILLING_APP_VERSION
    try:
        mock_info = VersionInfo(version="3.19.0", release_name="v3.19.0")
        monkeypatch.setattr(tracker, "fetch_remote_manifest", AsyncMock(return_value=mock_info))
        monkeypatch.setattr("app.claim.preview_plans",
                            AsyncMock(side_effect=ClaimError("ConnectError 32")))
        res = await tracker.check_and_sync()
        assert res["billing_updated"] is True
        assert constants.BILLING_APP_VERSION == "3.19.0"
    finally:
        constants.BILLING_APP_VERSION = billing_orig

    # 瞬态语义本身：ConnectError 文案 → True（不回滚）；上游拒绝文案 → False
    acc = fresh_app.find("zai", "probe-transient")
    assert acc is not None
    monkeypatch.setattr("app.claim.preview_plans",
                        AsyncMock(side_effect=ClaimError("HTTP 405 unusual activity")))
    assert await vt._billing_probe_ok() is False


@pytest.mark.asyncio
async def test_probe_skipped_when_no_candidate(fresh_app):
    """无 active 可探号 → 视为通过（没有实弹面就不阻塞跟版）。"""
    from app import version_tracker as vt

    assert await vt._billing_probe_ok() is True
