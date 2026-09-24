"""客户端指纹单测（2026-09-07 随机生成版）：合规性、分配幂等、换发、round-trip。"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict

import pytest

from app.fingerprint import DeviceProfile, profile_for, random_profile, rotate
from app.models import Account

GOOD_JWT = "h1.eyJzdWIiOiJhIn0.sig"

_VALID_PLATFORMS = {("darwin", "arm64"), ("darwin", "x64"), ("win32", "x64")}
_VALID_SCREENS = {"1920x1080", "2560x1440", "3840x2160", "2560x1600",
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

    def test_random_profile_is_coherent_sku(self):
        """平台×内核×分辨率必须是成套桌面 SKU，禁止字段笛卡尔积。"""
        from app import fingerprint

        for _ in range(80):
            p = random_profile()
            assert (p.platform, p.arch, p.os_version, p.screen) in fingerprint.sku_combos()
            assert p.platform in ("darwin", "win32")

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

    def test_assign_is_generated_desktop_sku_not_host_clone(self):
        """入池档案必须是成套桌面 SKU，禁止克隆部署机（linux 云内核 / 1920x1080 兜底）。"""
        from app import fingerprint, hostinfo

        acc = _acc("gen")
        p = profile_for(acc)
        host = hostinfo.collect_host_profile()
        assert (p.platform, p.arch, p.os_version, p.screen) in fingerprint.sku_combos()
        assert p.platform in ("darwin", "win32")
        assert p.device_mid != host.device_mid
        # 宿主机四元组若不是桌面 SKU（pxed linux 云内核），账号档案不得等于宿主机
        host_key = (host.platform, host.arch, host.os_version, host.screen)
        if host_key not in fingerprint.sku_combos():
            assert (p.platform, p.arch, p.os_version, p.screen) != host_key

    def test_assign_does_not_read_host_when_host_invalid(self, monkeypatch):
        """生成器不再依赖宿主机采集；hostinfo 抛错也不能阻断入池。"""
        import app.hostinfo as hostinfo
        from app import fingerprint

        def _boom():
            raise OSError("no host")

        monkeypatch.setattr(hostinfo, "collect_host_profile", _boom)
        p = fingerprint.assign(_acc("nohost"))
        assert (p.platform, p.arch, p.os_version, p.screen) in fingerprint.sku_combos()

    def test_distinct_accounts_usually_differ(self):
        """两账号至少 device_mid 不同；全套字段偶然全同不作为失败（SKU 池有限）。"""
        a, b = profile_for(_acc("x")), profile_for(_acc("y"))
        assert a.device_mid != b.device_mid

    def test_startup_backfills_host_clone_to_sku(self, fresh_app):
        """旧版 linux 云主机克隆启动时换成桌面 SKU，并清 installed_at 以便重装。"""
        from app import main as main_module
        from app.fingerprint import is_generated_sku, profile_for

        acc = fresh_app.add_account("zai", "old", "jwt.token.old")
        old_mid = "12345678-1234-4123-8123-123456789abc"
        acc.fingerprint = {
            "platform": "linux", "arch": "x64",
            "os_version": "5.10.134-18.0.11.lifsea8.x86_64",
            "language": "en-US", "timezone": "UTC", "screen": "1920x1080",
            "device_mid": old_mid,
        }
        acc.installed_at = 123.0
        fresh_app.update_account(acc)

        replaced = main_module._backfill_fingerprints()
        assert len(replaced) == 1
        after = fresh_app.find("zai", acc.id)
        p = profile_for(after)
        assert is_generated_sku(p)
        assert p.platform in ("darwin", "win32")
        assert p.device_mid != old_mid
        assert after.installed_at is None


class TestHostProfile:
    def test_host_profile_valid_and_uses_given_mid(self):
        from app.fingerprint import host_profile

        mid = "12345678-1234-4123-8123-123456789abc"
        p = host_profile(device_mid=mid)
        assert p.device_mid == mid
        assert p.platform in ("darwin", "win32", "linux")

    def test_host_profile_default_mid_is_uuid(self):
        from app.fingerprint import host_profile

        p = host_profile()
        import uuid as _uuid
        _uuid.UUID(p.device_mid)  # 不抛即合法

    def test_host_profile_rejects_bad_shape(self):
        from app import fingerprint

        broken = fingerprint.DeviceProfile(
            platform="darwin", arch="arm64", os_version="not a release",
            language="zh-CN", timezone="Asia/Shanghai", screen="1920x1080",
        )
        import pytest
        with pytest.raises(ValueError):
            fingerprint._validate(broken, host_real=True)

    def test_host_real_accepts_real_host_shapes(self):
        """host_real 平台按形态放行：linux/arm64 云主机是真机事实，不再被预置池拒绝。"""
        from app import fingerprint

        arm = fingerprint.DeviceProfile(
            platform="linux", arch="arm64", os_version="5.10.134-18.0.11.an8_arm64",
            language="en-US", timezone="UTC", screen="1920x1080",
        )
        fingerprint._validate(arm, host_real=True)  # 不抛即合规

        # 形态门仍在：非法取值照样拒绝（防止采集污染）
        bogus = fingerprint.DeviceProfile(
            platform="sunos", arch="mips", os_version="5.10", language="en-US",
            timezone="UTC", screen="1920x1080",
        )
        with pytest.raises(ValueError):
            fingerprint._validate(bogus, host_real=True)


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

    def test_rotate_stays_on_desktop_sku(self):
        from app import fingerprint

        acc = _acc("a")
        p = rotate(acc)
        assert (p.platform, p.arch, p.os_version, p.screen) in fingerprint.sku_combos()


@pytest.mark.parametrize("field", ["platform", "arch", "os_version", "language", "timezone", "screen"])
def test_profile_fields_complete(field: str):
    for _ in range(20):
        assert getattr(random_profile(), field)
