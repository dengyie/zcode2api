"""客户端指纹池单测（2026-09-07）：分配幂等、轮转、换发、dict round-trip。"""

from __future__ import annotations

import pytest

from app.fingerprint import _TEMPLATES, DeviceProfile, profile_for, rotate
from app.models import Account


def _acc(name: str) -> Account:
    return Account.create("zai", name, "h1.eyJzdWIiOiJhIn0.sig")


class TestAssign:
    def test_assign_sets_profile_with_unique_mid(self):
        acc = _acc("a")
        p1 = profile_for(acc)
        assert isinstance(p1, DeviceProfile)
        assert p1.device_mid
        assert p1.platform and p1.arch and p1.os_version

    def test_profile_for_idempotent(self):
        acc = _acc("a")
        p1 = profile_for(acc)
        p2 = profile_for(acc)
        assert p1 is p2  # 已有档案原样返回，不重新分配

    def test_mids_unique_across_accounts(self):
        mids = {profile_for(_acc(f"acc-{i}")).device_mid for i in range(12)}
        assert len(mids) == 12  # device_mid 永不复用

    def test_templates_cover_multiple_platforms(self):
        platforms = {tpl.platform for tpl in _TEMPLATES}
        assert platforms == {"darwin", "win32", "linux"}

    def test_os_category_mapping(self):
        assert DeviceProfile("darwin", "arm64", "25.5.0", "zh-CN",
                             "Asia/Shanghai", "2560x1440").os_category == "macos"
        assert DeviceProfile("win32", "x64", "10.0.22631", "zh-CN",
                             "Asia/Shanghai", "1920x1080").os_category == "windows"
        assert DeviceProfile("linux", "x64", "6.8.0", "en-US",
                             "UTC", "1920x1080").os_category == "linux"


class TestPersistRoundTrip:
    def test_dict_round_trip_preserves_profile(self):
        from dataclasses import asdict

        acc = _acc("a")
        original = profile_for(acc)
        acc.fingerprint = {
            "platform": original.platform, "arch": original.arch,
            "os_version": original.os_version, "language": original.language,
            "timezone": original.timezone, "screen": original.screen,
            "device_mid": original.device_mid,
        }
        restored = Account.from_dict(asdict(acc))
        p = profile_for(restored)
        assert p.device_mid == original.device_mid
        assert p.platform == original.platform

    def test_none_fingerprint_triggers_fresh_assign(self):
        acc = _acc("a")
        acc.fingerprint = None
        p = profile_for(acc)
        assert isinstance(p, DeviceProfile) and p.device_mid


class TestRotate:
    def test_rotate_changes_mid(self):
        acc = _acc("a")
        p1 = profile_for(acc)
        p2 = rotate(acc)
        assert p2.device_mid != p1.device_mid
        assert profile_for(acc) is p2  # 换发后再次取用稳定

    def test_rotate_cycles_templates(self):
        acc = _acc("a")
        seen = {rotate(acc).platform for _ in range(len(_TEMPLATES))}
        assert len(seen) >= 2  # 换发一轮覆盖多套模板


@pytest.mark.parametrize("field", ["platform", "arch", "os_version", "language", "timezone", "screen"])
def test_profile_fields_complete(field: str):
    for tpl in _TEMPLATES:
        assert getattr(tpl, field), f"模板缺字段 {field}"
