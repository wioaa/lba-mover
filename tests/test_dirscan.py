"""未提权扫描路径（目录遍历 + 逐文件簇映射）。

这条路径的价值只有一个：**没有管理员权限时也能扫出真实分布**。
所以用例的重点不是"能跑通"，而是"三件容易错的事都做对了"：

1. 稀疏洞（``lcn == -1``）必须跳过，否则会把文件的簇算到别处；
2. VCN 顺序 ≠ LCN 顺序，结果必须重新排序（``FileFrag`` 要求升序）；
3. 重叠 run 必须合并，不能因为一个特殊文件把整卷扫描打断。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import VolumeKind
from lbamover.contract.models import Extent, Geometry, VolumeInfo
from lbamover.platform.dirscan import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    DirectoryCatalog,
    _runs_to_extents,
)

GEO = Geometry(4096, 1000)


def volume(blocks: int = 1000, fs: str = "NTFS") -> VolumeInfo:
    return VolumeInfo(
        device_path=r"\\.\Q:",
        mount_point="Q:\\",
        label="t",
        filesystem=fs,
        kind=VolumeKind.FIXED,
        geometry=Geometry(4096, blocks),
        free_bytes=blocks * 4096 // 2,
        writable=True,
    )


def fake_scanner(entries):
    """entries: [(path, attributes, size)]"""

    def _scan(_mount):
        yield from entries

    return _scan


class TestRunsToExtents:
    def test_basic(self):
        got = _runs_to_extents(((0, 100, 5), (5, 200, 3)), GEO)
        assert got == (Extent(100, 5), Extent(200, 3))

    def test_sparse_hole_skipped(self):
        # VCN 0..4 → LCN 100；VCN 5..9 是洞；VCN 10..12 → LCN 300
        got = _runs_to_extents(((0, 100, 5), (5, -1, 5), (10, 300, 3)), GEO)
        assert got == (Extent(100, 5), Extent(300, 3))

    def test_lcn_can_move_backwards_and_must_sort(self):
        # 后半段落在前面的空闲区：LCN 递减，但 FileFrag 要求升序
        got = _runs_to_extents(((0, 500, 4), (4, 200, 2)), GEO)
        assert got == (Extent(200, 2), Extent(500, 4))

    def test_overlapping_runs_are_merged_not_rejected(self):
        # 事务日志类文件会给出首尾重叠的 run；直接交给 FileFrag 会抛错，
        # 进而把整卷扫描打断
        got = _runs_to_extents(((0, 100, 10), (10, 105, 10)), GEO)
        assert got == (Extent(100, 15),)

    def test_adjacent_runs_merged(self):
        got = _runs_to_extents(((0, 100, 5), (5, 105, 5)), GEO)
        assert got == (Extent(100, 10),)

    def test_empty(self):
        assert _runs_to_extents((), GEO) == ()


class TestDirectoryCatalog:
    def _catalog(self, entries, runs: dict[str, tuple], **kw):
        return DirectoryCatalog(
            volume(),
            scanner=fake_scanner(entries),
            query=lambda p: runs.get(p, ()),
            **kw,
        )

    def test_skips_directories_and_reparse_points(self):
        cat = self._catalog(
            [
                ("Q:\\dir", FILE_ATTRIBUTE_DIRECTORY, 0),
                ("Q:\\link", FILE_ATTRIBUTE_REPARSE_POINT, 4096),
                ("Q:\\a.bin", 0, 8192),
            ],
            {"Q:\\a.bin": ((0, 10, 2),)},
        )
        files = cat.list_files()
        assert [f.path for f in files] == ["Q:\\a.bin"]
        assert files[0].extents == (Extent(10, 2),)
        assert "目录 1" in cat.stats.notes[0]
        assert "重解析点 1" in cat.stats.notes[0]

    def test_zero_size_files_are_not_queried(self):
        calls: list[str] = []

        def query(path):
            calls.append(path)
            return ()

        cat = DirectoryCatalog(
            volume(),
            scanner=fake_scanner([("Q:\\empty", 0, 0), ("Q:\\a", 0, 4096)]),
            query=query,
        )
        cat.list_files()
        assert calls == ["Q:\\a"]

    def test_failure_of_one_file_does_not_abort(self):
        def query(path):
            if path.endswith("bad"):
                raise OSError("拒绝访问")
            return ((0, 10, 1),)

        cat = DirectoryCatalog(
            volume(),
            scanner=fake_scanner([("Q:\\bad", 0, 4096), ("Q:\\ok", 0, 4096)]),
            query=query,
        )
        files = cat.list_files()
        assert [f.path for f in files] == ["Q:\\ok"]
        assert cat.stats.records_bad == 1
        assert any("只允许预览" in n for n in cat.stats.notes)

    def test_free_space_is_the_complement(self):
        cat = self._catalog(
            [("Q:\\a", 0, 4096), ("Q:\\b", 0, 4096)],
            {"Q:\\a": ((0, 0, 10),), "Q:\\b": ((0, 20, 10),)},
        )
        files = cat.list_files()
        free = cat.free_extents()
        assert sum(e.length for e in free) == 1000 - 20
        assert free[0].start == 10 and free[0].end == 20
        assert sum(f.total_blocks for f in files) == 20

    def test_is_always_restricted(self):
        cat = self._catalog([], {})
        cat.list_files()
        assert cat.restricted is True
        assert cat.stats.free_source == "complement"
        assert cat.stats.kind == "遍历"

    def test_max_files_truncates_and_says_so(self):
        entries = [(f"Q:\\f{i}", 0, 4096) for i in range(10)]
        runs = {f"Q:\\f{i}": ((0, i * 10, 5),) for i in range(10)}
        cat = self._catalog(entries, runs, max_files=3)
        assert len(cat.list_files()) == 3
        assert any("上限" in n for n in cat.stats.notes)

    def test_scan_is_cached(self):
        calls = {"n": 0}

        def query(path):
            calls["n"] += 1
            return ((0, 10, 1),)

        cat = DirectoryCatalog(
            volume(), scanner=fake_scanner([("Q:\\a", 0, 4096)]), query=query
        )
        cat.list_files()
        cat.list_files()
        cat.free_extents()
        assert calls["n"] == 1

    def test_progress_called(self):
        seen: list[int] = []
        entries = [(f"Q:\\f{i}", 0, 4096) for i in range(10)]
        runs = {f"Q:\\f{i}": ((0, i * 10, 1),) for i in range(10)}
        cat = DirectoryCatalog(
            volume(),
            scanner=fake_scanner(entries),
            query=lambda p: runs.get(p, ()),
            on_progress=lambda n, p: seen.append(n),
            progress_every=4,
        )
        cat.list_files()
        assert seen and seen[0] >= 4

    def test_walk_error_becomes_device_unavailable(self):
        def boom(_mount):
            raise OSError("盘不见了")
            yield  # pragma: no cover

        cat = DirectoryCatalog(volume(), scanner=boom)
        from lbamover.platform.errors import DeviceUnavailable

        with pytest.raises(DeviceUnavailable):
            cat.list_files()

    def test_files_sorted_by_first_extent(self):
        cat = self._catalog(
            [("Q:\\late", 0, 4096), ("Q:\\early", 0, 4096)],
            {"Q:\\late": ((0, 900, 1),), "Q:\\early": ((0, 100, 1),)},
        )
        files = cat.list_files()
        assert [f.path for f in files] == ["Q:\\early", "Q:\\late"]
