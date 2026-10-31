"""保护层：哪些文件不许动、搬完还剩多少空间。

这层的每一条规则都对应一种"搬错了就出事"的具体情形，因此用例按情形写，
而不是按函数写。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import Strategy, VolumeKind
from lbamover.contract.models import (
    Extent,
    FileFrag,
    FragReport,
    Geometry,
    MoveOp,
    Plan,
    VolumeInfo,
)
from lbamover.engine.safety import DEFAULT_POLICY, ProtectionPolicy, audit_plan


def frag(fid: str, path: str, attrs: int = 0, blocks: int = 8) -> FileFrag:
    return FileFrag(fid, path, (Extent(0, blocks),), attributes=attrs)


def report(files, total=1000, free=800) -> FragReport:
    return FragReport(
        geometry=Geometry(4096, total),
        files=tuple(files),
        free_extents=(Extent(total - free, free),),
    )


def volume(is_system: bool = False) -> VolumeInfo:
    return VolumeInfo(
        device_path="\\\\?\\Volume{test}",
        mount_point="D:\\",
        label="t",
        filesystem="NTFS",
        kind=VolumeKind.FIXED,
        geometry=Geometry(4096, 1000),
        is_system=is_system,
        writable=True,
    )


class TestVerdicts:
    @pytest.mark.parametrize(
        "fid,path,attrs,expected",
        [
            ("3", "$MFT", 0, "系统元文件"),
            ("99", r"D:\Windows\System32\drivers\a.sys", 0, "受保护目录"),
            ("99", r"D:\Windows\WinSxS\amd64\x.dll", 0, "受保护目录"),
            ("99", r"D:\$Recycle.Bin\S-1-5-21\x.dat", 0, "受保护目录"),
            ("99", r"D:\System Volume Information\tracking.log", 0, "受保护目录"),
            ("99", r"D:\pagefile.sys", 0, "受保护文件名"),
            ("99", r"D:\hiberfil.sys", 0, "受保护文件名"),
            ("99", r"D:\data\a.bin", 0x0004, "系统属性文件"),
        ],
    )
    def test_blocked(self, fid, path, attrs, expected):
        assert expected in (DEFAULT_POLICY.verdict(frag(fid, path, attrs)) or "")

    @pytest.mark.parametrize(
        "path",
        [r"D:\data\alpha.bin", r"D:\Users\me\video.mp4", r"D:\Games\x\y.pak"],
    )
    def test_allowed(self, path):
        assert DEFAULT_POLICY.verdict(frag("99", path)) is None

    def test_drive_letter_does_not_affect_matching(self):
        a = DEFAULT_POLICY.verdict(frag("99", r"C:\Windows\System32\a.dll"))
        b = DEFAULT_POLICY.verdict(frag("99", r"D:\Windows\System32\a.dll"))
        assert a is not None and a == b

    def test_hidden_only_when_enabled(self):
        f = frag("99", r"D:\data\secret.bin", attrs=0x0002)
        assert DEFAULT_POLICY.verdict(f) is None
        strict = ProtectionPolicy(protect_hidden_attr=True)
        assert strict.verdict(f) == "隐藏文件"


class TestScreen:
    def test_splits_without_dropping(self):
        files = (
            frag("16", r"D:\data\alpha.bin"),
            frag("17", r"D:\pagefile.sys"),
            frag("18", r"D:\Windows\System32\a.dll"),
        )
        allowed, blocked = DEFAULT_POLICY.screen(files)
        assert [f.file_id for f in allowed] == ["16"]
        assert len(blocked) == 2
        assert len(allowed) + len(blocked) == len(files)

    def test_empty_input(self):
        assert DEFAULT_POLICY.screen(()) == ((), [])


class TestVolumeVerdict:
    def test_system_volume_allowed_by_default(self):
        # 整理 C: 是主要用途，默认放行；闸门在界面的手输确认
        assert DEFAULT_POLICY.volume_verdict(volume(True)) is None

    def test_strict_policy_blocks_system_volume(self):
        strict = ProtectionPolicy(block_system_volume=True)
        assert strict.volume_verdict(volume(True)) is not None
        assert strict.volume_verdict(volume(False)) is None


class TestAuditPlan:
    def _plan(self, ops) -> Plan:
        return Plan(job_id="j", strategy=Strategy.COALESCE, ops=tuple(ops))

    def test_passes_for_clean_plan(self):
        files = [frag("16", r"D:\data\alpha.bin")]
        plan = self._plan([MoveOp(0, "16", r"D:\data\alpha.bin", (Extent(0, 8),), 100)])
        audit = audit_plan(plan, report(files), DEFAULT_POLICY, volume=volume())
        assert audit.allowed, audit.reasons
        assert audit.free_after_blocks == 800 - 8

    def test_blocks_protected_file(self):
        files = [frag("16", r"D:\pagefile.sys")]
        plan = self._plan([MoveOp(0, "16", r"D:\pagefile.sys", (Extent(0, 8),), 100)])
        audit = audit_plan(plan, report(files), DEFAULT_POLICY, volume=volume())
        assert not audit.allowed
        assert any("受保护" in w for _, w in audit.blockers)

    def test_blocks_expired_plan(self):
        # 计划里的文件在扫描结果里找不到 = 计划过期，不能凭它写盘
        plan = self._plan([MoveOp(0, "999", r"D:\gone.bin", (Extent(0, 8),), 100)])
        audit = audit_plan(plan, report([]), DEFAULT_POLICY, volume=volume())
        assert not audit.allowed
        assert any("计划已过期" in w for _, w in audit.blockers)

    def test_blocks_when_free_space_below_floor(self):
        files = [frag("16", r"D:\data\alpha.bin", blocks=100)]
        plan = self._plan([MoveOp(0, "16", r"D:\data\alpha.bin", (Extent(0, 100),), 900)])
        audit = audit_plan(
            plan, report(files, total=1000, free=110), DEFAULT_POLICY, volume=volume()
        )
        assert not audit.allowed
        assert any("空闲" in r for r in audit.reasons)

    def test_absolute_floor_never_exceeds_volume(self):
        """小卷上"至少留 4096 块"是不可能满足的，不能因此判死。"""
        files = [frag("16", r"D:\data\alpha.bin")]
        plan = self._plan([MoveOp(0, "16", r"D:\data\alpha.bin", (Extent(0, 8),), 100)])
        report_small = FragReport(
            geometry=Geometry(512, 200),
            files=tuple(files),
            free_extents=(Extent(100, 100),),
        )
        audit = audit_plan(plan, report_small, DEFAULT_POLICY)
        assert audit.required_free_blocks <= 20

    def test_strict_volume_policy_blocks_all(self):
        strict = ProtectionPolicy(block_system_volume=True)
        files = [frag("16", r"D:\data\alpha.bin")]
        plan = self._plan([MoveOp(0, "16", r"D:\data\alpha.bin", (Extent(0, 8),), 100)])
        audit = audit_plan(plan, report(files), strict, volume=volume(is_system=True))
        assert not audit.allowed
        assert any("系统卷" in r for r in audit.reasons)
