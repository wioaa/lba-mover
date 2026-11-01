"""目录服务的选择逻辑，以及"受限报告不许执行"这条闸门。

选择逻辑值得单独测：它同时决定"能不能扫"和"扫出来的东西能不能用来改"。
二者混淆过一次——未提权时直接报错说读不了卷，用户点第一次扫描就卡死在那里。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import Strategy, VolumeKind
from lbamover.contract.models import Extent, FileFrag, Geometry, MoveOp, Plan, VolumeInfo
from lbamover.devices.base import DeviceError, ErrorClass
from lbamover.engine.catalog import CatalogError, open_ntfs_catalog
from lbamover.engine.engine import EngineCore
from lbamover.engine.errors import PlanRejected
from lbamover.platform.dirscan import DirectoryCatalog
from lbamover.platform.errors import DeviceUnavailable


def volume(fs: str = "NTFS") -> VolumeInfo:
    return VolumeInfo(
        device_path=r"\\.\Q:",
        mount_point="Q:\\",
        label="t",
        filesystem=fs,
        kind=VolumeKind.FIXED,
        geometry=Geometry(4096, 1000),
        free_bytes=500 * 4096,
        writable=True,
    )


@pytest.fixture
def deny(monkeypatch):
    """让打开原始卷必定失败，模拟未提权。"""

    def _raise(*a, **kw):
        raise DeviceUnavailable("打开 \\\\.\\Q: 失败：拒绝访问。", winerror=5)

    monkeypatch.setattr("lbamover.devices.open_device", _raise, raising=True)


class TestCatalogSelection:
    def test_permission_denied_falls_back_to_walk(self, deny):
        cat = open_ntfs_catalog(volume())
        assert isinstance(cat, DirectoryCatalog)
        assert cat.restricted is True

    def test_device_error_access_denied_also_falls_back(self, monkeypatch):
        def _raise(*a, **kw):
            raise DeviceError(
                "打开失败：拒绝访问。", error_class=ErrorClass.FATAL, winerror=5
            )

        monkeypatch.setattr("lbamover.devices.open_device", _raise, raising=True)
        assert isinstance(open_ntfs_catalog(volume()), DirectoryCatalog)

    def test_other_errors_are_not_swallowed(self, monkeypatch):
        def _raise(*a, **kw):
            raise DeviceUnavailable("设备不存在", winerror=2)

        monkeypatch.setattr("lbamover.devices.open_device", _raise, raising=True)
        with pytest.raises(CatalogError):
            open_ntfs_catalog(volume())

    def test_non_ntfs_goes_straight_to_walk(self, monkeypatch):
        def _boom(*a, **kw):  # 不该被调用：exFAT 没有 $MFT
            raise AssertionError("不应尝试读 MFT")

        monkeypatch.setattr("lbamover.devices.open_device", _boom, raising=True)
        cat = open_ntfs_catalog(volume("exFAT"))
        assert isinstance(cat, DirectoryCatalog)


class FakeCatalog:
    """最小目录服务：给引擎用。"""

    def __init__(self, files, free, *, restricted: bool):
        self._files = tuple(files)
        self._free = tuple(free)
        self.restricted = restricted

    def list_files(self, _volume=None):
        return self._files

    def free_extents(self, _volume=None):
        return self._free

    def close(self):
        pass


def make_plan() -> Plan:
    return Plan(
        job_id="j",
        strategy=Strategy.COALESCE,
        ops=(MoveOp(0, "1", "Q:\\a.bin", (Extent(0, 4),), dst_start=100),),
    )


class TestRestrictedGuard:
    def _core(self, restricted: bool) -> EngineCore:
        files = [FileFrag("1", "Q:\\a.bin", (Extent(0, 4),))]
        free = [Extent(500, 400)]
        core = EngineCore(
            catalog_factory=lambda vol, on_progress=None: FakeCatalog(
                files, free, restricted=restricted
            )
        )
        core.set_target(volume())
        core.scan()
        return core

    def test_report_carries_the_flag(self):
        assert self._core(True).report is not None
        assert self._core(True).report.restricted is True
        assert self._core(False).report.restricted is False

    def test_restricted_report_cannot_be_executed(self):
        core = self._core(True)
        with pytest.raises(PlanRejected) as exc:
            core.execute(make_plan())
        assert "未提权" in str(exc.value) or "受限" in str(exc.value)

    def test_full_report_passes_the_guard(self):
        """非受限报告不该被"未提权"闸门拦住（后面因假设备失败是另一回事）。"""
        core = self._core(False)
        try:
            core.execute(make_plan(), device=object())
        except Exception as exc:  # noqa: BLE001 - 闸门已放行，后面怎么失败都行
            assert "未提权" not in str(exc)
            assert "受限" not in str(exc)
