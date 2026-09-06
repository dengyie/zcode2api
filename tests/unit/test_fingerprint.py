"""客户端指纹单测（2026-09-07 随机生成版）：合规性、分配幂等、换发、round-trip。"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict

import pytest

from app.fingerprint import DeviceProfile, profile_for, random_profile, rotate
from app.models import Account

GOOD_JWT = "h1.eyJzdWIiOiJhIn0.sig"

_VALID_PLATFORMS = {("darwin", "arm64"), ("darwin", "x64"), ("win32", "x64"), ("linux", "x64")}
_VALID_SCREENS = {"1920x1080", "2560x1440", "3840x2160", "5120x2880", "2560x1600",
                  "1728x1117", "1512x982", "1440x900", "1366x768"}
# os.release() 形态门（按平台），与常量池解耦：上游只看形态真实性
_OS_SHAPE = {
    "darwin": r"^2[2-9]\.\d+\.\d+$",
    "win32": r"^10\.0\.\d{5}$",
    "linux": r"^\d+\.\d+\.\d+",
}


def _acc(name: str) -> Account:
    return Account.create("zai", name, GOOD_JWT)


class TestRandomProfile:
    def test_platform_combos_real(self):
        for _ in range(50):
            p = random_profile()
            assert (p.platform, p.arch) in _VALID_PLATFORMS

    def test_os_version_shape_matches_platform(self):
        for _ in range(50):
            p = random_profile()
            assert re.match(_OS_SHAPE[p.platform], p.os_version), p

    def test_locale_pairs_realistic(self):
        for _ in range(50):
            p = random_profile()
            assert p.timezone.split("/")[0] in ("Asia", "America", "Europe")
            if p.language == "zh-CN":
                assert p.timezone == "Asia/Shanghai"

    def test_screen_resolution_common(self):
        for _ in range(50):
            assert random_profile().screen in _VALID_SCREENS

    def test_device_mid_uuid_v4_unique(self):
        mids = {random_profile().device_mid for _ in range(50)}
        assert len(mids) == 50
        for mid in mids:
            assert uuid.UUID(mid).version == 4

    def test_os_category_mapping(self):
        assert DeviceProfile("darwin", "arm64", "25.5.0", "zh-CN",
                             "Asia/Shanghai", "2560x1440").os_category == "macos"
        assert DeviceProfile("win32", "x64", "10.0.22631", "zh-CN",
                             "Asia/Shanghai", "1920x1080").os_category == "windows"
        assert DeviceProfile("linux", "x64", "6.8.0", "en-US",
                             "UTC", "1920x1080").os_category == "linux"


class TestAssign:
    def test_assign_sets_profile(self):
        acc = _acc("a")
        p1 = profile_for(acc)
        assert isinstance(p1, DeviceProfile) and p1.device_mid

    def test_profile_for_idempotent(self):
        acc = _acc("a")
        assert profile_for(acc) is profile_for(acc)

    def test_mids_unique_across_accounts(self):
        mids = {profile_for(_acc(f"acc-{i}")).device_mid for i in range(20)}
        assert len(mids) == 20  # device_mid 永不复用

    def test_distinct_accounts_usually_differ(self):
        """随机池下两账号档案全同概率极低（组合空间 >10^4）。"""
        a, b = profile_for(_acc("x")), profile_for(_acc("y"))
        assert (a.platform, a.os_version, a.device_mid) != (b.platform, b.os_version, b.device_mid)


class TestPersistRoundTrip:
    def test_dict_round_trip_preserves_profile(self):
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
        assert isinstance(profile_for(acc), DeviceProfile)


class TestRotate:
    def test_rotate_changes_mid(self):
        acc = _acc("a")
        p1 = profile_for(acc)
        p2 = rotate(acc)
        assert p2.device_mid != p1.device_mid
        assert profile_for(acc) is p2

    def test_rotate_gives_fresh_device_mid_every_time(self):
        acc = _acc("a")
        mids = {rotate(acc).device_mid for _ in range(10)}
        assert len(mids) == 10


@pytest.mark.parametrize("field", ["platform", "arch", "os_version", "language", "timezone", "screen"])
def test_profile_fields_complete(field: str):
    for _ in range(20):
        assert getattr(random_profile(), field)
