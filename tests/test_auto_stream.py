"""1.0.0F 分块流式自动模式的挑选函数回归（``densify.auto_stream_selection``）。

只测「挑谁」，不测落点——落点由文件系统在空闲区分配（流式迁移的语义）。
关键断言：大文件优先、容量上限、放不下就跳过后继续挑、不可移动排除、不限容量时的兜底。

第二节是 D-009 回归钉：分块流式搬迁的临时文件（``<源>.lbastream.tmp``）残留管理。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.densify import auto_stream_selection  # noqa: E402
from app.core.models import Extent, FileEntry  # noqa: E402

BPC = 4096
MIB = 1024 * 1024


def _entry(path: str, clusters: int, *, movable: bool = True, reason: str = "") -> FileEntry:
    return FileEntry(
        path=path,
        name=path.rsplit("\\", 1)[-1],
        size=clusters * BPC,
        extents=[Extent(vcn=0, lcn=0, clusters=clusters)],
        movable=movable,
        reason="" if movable else (reason or "不可移动"),
    )


def test_largest_first_order() -> None:
    small = _entry("R:\\a.bin", 10)
    huge = _entry("R:\\b.bin", 900)
    mid = _entry("R:\\c.bin", 100)
    pick = auto_stream_selection([small, huge, mid], bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\b.bin", "R:\\c.bin", "R:\\a.bin"]


def test_capacity_is_hard_limit() -> None:
    a = _entry("R:\\a.bin", 100)     # 100 * 4096 = 400 KiB
    b = _entry("R:\\b.bin", 100)
    limit = 400 * 1024 + 100 * 1024  # 只够 a + 四分之一 b（放不下就跳过）
    pick = auto_stream_selection([a, b], capacity_bytes=limit, bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\a.bin"]
    assert pick.skipped_oversize == 1
    assert pick.total_bytes <= limit


def test_oversize_skipped_then_smaller_taken() -> None:
    """放不下的大文件被跳过，但更小的文件仍会被挑进来（不是停止挑选）。"""
    huge = _entry("R:\\huge.bin", 5000)      # ~20 MiB
    small = _entry("R:\\small.bin", 100)     # 400 KiB
    limit = 2 * MIB
    pick = auto_stream_selection([huge, small], capacity_bytes=limit, bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\small.bin"]
    assert pick.skipped_oversize == 1
    assert "填充率" in pick.note


def test_unmovable_and_zero_cluster_excluded() -> None:
    ok = _entry("R:\\ok.bin", 50)
    locked = _entry("R:\\locked.bin", 9000, movable=False, reason="被独占")
    empty = _entry("R:\\empty.bin", 0)
    pick = auto_stream_selection([ok, locked, empty], bytes_per_cluster=BPC)
    assert [e.path for e in pick.selected] == ["R:\\ok.bin"]
    assert pick.skipped_unmovable == 2


def test_no_capacity_falls_back_to_free_bytes() -> None:
    """未设容量 → 以卷可用空间为兜底上限。"""
    a = _entry("R:\\a.bin", 100)   # 400 KiB
    b = _entry("R:\\b.bin", 100)   # 400 KiB
    free = 500 * 1024              # 只够一个
    pick = auto_stream_selection([a, b], bytes_per_cluster=BPC, free_bytes=free)
    assert pick.capacity_bytes == free
    assert len(pick.selected) == 1


def test_no_capacity_and_no_free_is_unbounded() -> None:
    a = _entry("R:\\a.bin", 10)
    pick = auto_stream_selection([a], bytes_per_cluster=BPC, free_bytes=0)
    assert pick.capacity_bytes == 0        # 0 = 不限
    assert len(pick.selected) == 1
    assert "未设容量上限" in pick.note


def test_min_clusters_filter() -> None:
    big = _entry("R:\\big.bin", 500)
    tiny = _entry("R:\\tiny.bin", 5)
    pick = auto_stream_selection([big, tiny], bytes_per_cluster=BPC, min_clusters=100)
    assert [e.path for e in pick.selected] == ["R:\\big.bin"]
    assert pick.skipped_unmovable == 1


def test_empty_input_is_safe() -> None:
    pick = auto_stream_selection([], bytes_per_cluster=BPC)
    assert pick.selected == []
    assert pick.total_bytes == 0
    assert pick.note


# =========================================================================== #
# D-009 回归钉：``<源>.lbastream.tmp`` 残留管理
#
# 用 ``app.win32.file_io`` 的**替身**跑 BlockStreamWorker.run()：替身只表达
# 「整份拷贝」与「移到回收站」两件事，并记录每次 copy_stream 进入时目的文件
# 是否已存在 —— 后者正是缺陷里说的"向已存在的 tmp 继续写"的现场证据。
# =========================================================================== #
class _FakeFileIO:
    """``app.win32.file_io`` 的最小替身（不动用户回收站、不做真实扇区读写）。"""

    def __init__(self, root: str) -> None:
        self._root = root
        self.copy_calls: list = []      # (src, dst, dst_existed_before_copy)
        self.recycled: list = []

    # 签名与真实 file_io.copy_stream 保持一致
    def copy_stream(
        self,
        src_path,
        dst_path,
        *,
        mode="memory",
        cache_dir=None,
        buffer_blocks=2,
        bytes_per_cluster=BPC,
        block_clusters=65536,
        preallocate_dst=False,
    ):
        existed = os.path.exists(dst_path)
        self.copy_calls.append((src_path, dst_path, existed))
        with open(src_path, "rb") as reader:
            data = reader.read()
        with open(dst_path, "wb") as writer:   # 与真实 preallocate_dst 的覆盖语义等价
            writer.write(data)
        return len(data), 1

    def recycle(self, path: str) -> bool:
        bin_dir = os.path.join(self._root, "_recycle")
        os.makedirs(bin_dir, exist_ok=True)
        target = os.path.join(bin_dir, f"{len(self.recycled)}-{os.path.basename(path)}")
        os.replace(path, target)
        self.recycled.append(target)
        return True

    def default_scratch_dir(self, src_path: str) -> str:
        folder = os.path.join(self._root, "_scratch")
        os.makedirs(folder, exist_ok=True)
        return folder


def _run_stream_worker(entries: list, fake) -> dict:
    """同步跑一次 ``BlockStreamWorker``（file_io 已换成替身），返回完成摘要。"""
    from PySide6.QtWidgets import QApplication

    import app.win32 as win32_pkg
    from app.core.scanner import BlockStreamWorker

    _ = QApplication.instance() or QApplication([])
    summary: dict = {}
    worker = BlockStreamWorker(
        entries, mode="memory", buffer_blocks=2,
        bytes_per_cluster=512, block_clusters=4,
    )
    worker.completed.connect(lambda payload: summary.update(payload))

    original_had = hasattr(win32_pkg, "file_io")
    original = getattr(win32_pkg, "file_io", None)
    win32_pkg.file_io = fake
    try:
        worker.run()
    finally:
        if original_had:
            win32_pkg.file_io = original
        else:
            try:
                delattr(win32_pkg, "file_io")
            except AttributeError:  # pragma: no cover - 兜底
                pass
    return summary


def test_resolve_stream_residue_branches() -> None:
    """三种形态：无残留（空串）/ 源仍在（回收重复）/ 源已不在（补完改名）。"""
    from app.core.scanner import STREAM_TMP_SUFFIX, resolve_stream_residue

    with tempfile.TemporaryDirectory() as folder:
        fake = _FakeFileIO(folder)
        data = b"REAL" * 100
        src = os.path.join(folder, "a.bin")
        tmp = src + STREAM_TMP_SUFFIX

        # ① 没有残留 ⇒ 什么都不做
        with open(src, "wb") as fh:
            fh.write(data)
        assert resolve_stream_residue(src, tmp, fake.recycle) == ""
        assert fake.recycled == []

        # ② 源仍在 + 残留 ⇒ 回收残留（源不许动）
        with open(tmp, "wb") as fh:
            fh.write(b"STALE" * 90)
        note = resolve_stream_residue(src, tmp, fake.recycle)
        assert note and ("清理" in note or "临时" in note), note
        assert not os.path.exists(tmp), "残留必须被清掉"
        with open(src, "rb") as fh:
            assert fh.read() == data, "源文件本身不许被改动"
        assert len(fake.recycled) == 1

        # ③ 源已不在（源已入回收站、改名没来得及做）⇒ **补完改名**，绝不删
        survived = b"SURVIVOR" * 60
        src2 = os.path.join(folder, "b.bin")
        tmp2 = src2 + STREAM_TMP_SUFFIX
        with open(tmp2, "wb") as fh:
            fh.write(survived)
        note2 = resolve_stream_residue(src2, tmp2, fake.recycle)
        assert note2 and "改名" in note2, note2
        assert os.path.exists(src2), "唯一幸存副本必须被改回原名（不能删）"
        with open(src2, "rb") as fh:
            assert fh.read() == survived
        assert not os.path.exists(tmp2)
        assert tmp2 not in fake.recycled, "幸存副本绝不能被回收/删除"


def test_stream_worker_clears_stale_tmp_before_copy() -> None:
    """源还在、tmp 有残留：进 ``copy_stream`` 之前必须先把残留清掉。"""
    from app.core.scanner import STREAM_TMP_SUFFIX

    with tempfile.TemporaryDirectory() as folder:
        fake = _FakeFileIO(folder)
        data = b"REAL-DATA" * 400
        src = os.path.join(folder, "a.bin")
        with open(src, "wb") as fh:
            fh.write(data)
        tmp = src + STREAM_TMP_SUFFIX
        with open(tmp, "wb") as fh:
            fh.write(b"STALE-RESIDUE" * 900)   # 上次进程被杀留下的残留

        entries = [
            FileEntry(
                path=src, name="a.bin", size=len(data),
                extents=[Extent(vcn=0, lcn=0, clusters=4)], movable=True,
            )
        ]
        summary = _run_stream_worker(entries, fake)

        # ★ 判别点：绝不允许在**已存在**的 tmp 上开始拷贝
        assert fake.copy_calls, "应当发生过一次拷贝"
        assert [existed for _s, _d, existed in fake.copy_calls] == [False], (
            "D-009：copy_stream 不得在残留 tmp 上继续写（旧数据可能污染新内容）"
        )
        assert not os.path.exists(tmp), "同卷迁移完成后不得留下 .lbastream.tmp"
        with open(src, "rb") as fh:
            assert fh.read() == data, "源内容必须完整"
        assert summary.get("moved") == 1 and summary.get("failed") == 0, summary
        leftovers = [n for n in os.listdir(folder) if n.endswith(STREAM_TMP_SUFFIX)]
        assert leftovers == [], f"源目录仍有 tmp 残留：{leftovers}"


def test_stream_worker_recovers_source_from_interrupted_rename() -> None:
    """崩溃窗口：源已入回收站、``os.replace`` 没来得及做 —— 只剩 tmp。

    再跑一次必须把这份**唯一幸存副本**改回原名（而不是删除它，也不是报个小错
    就把用户数据留在陌生文件名下）。
    """
    from app.core.scanner import STREAM_TMP_SUFFIX

    with tempfile.TemporaryDirectory() as folder:
        fake = _FakeFileIO(folder)
        data = b"SURVIVOR-DATA" * 300
        src = os.path.join(folder, "c.bin")
        tmp = src + STREAM_TMP_SUFFIX
        with open(tmp, "wb") as fh:            # 源已被回收，只剩这份拷贝
            fh.write(data)

        entries = [
            FileEntry(
                path=src, name="c.bin", size=len(data),
                extents=[Extent(vcn=0, lcn=0, clusters=4)], movable=True,
            )
        ]
        summary = _run_stream_worker(entries, fake)

        assert os.path.exists(src), "必须把幸存副本改回源路径（用户数据不能丢）"
        with open(src, "rb") as fh:
            assert fh.read() == data
        assert not os.path.exists(tmp)
        assert tmp not in fake.recycled, "幸存副本绝不能进回收站"
        assert summary.get("moved") == 1 and summary.get("failed") == 0, summary
        assert [existed for _s, _d, existed in fake.copy_calls] == [False]


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
