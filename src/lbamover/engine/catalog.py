"""文件目录服务：告诉引擎「卷上有哪些文件、各占哪些 LBA」。

三种实现，按场景选：

* :class:`StaticCatalog` —— 内存里的表。测试、冒烟、演示、以及"从 JSON
  布局文件驱动一次离线整理"都用它。
* :class:`JsonCatalog` —— 从磁盘上的 JSON 读布局，便于对卷镜像做离线演练。
* :func:`open_ntfs_catalog` —— **真机 NTFS 卷的入口**。只读打开卷，交给
  :class:`lbamover.ntfs.MftCatalog` 解析 ``$MFT``，得到「文件 → 簇区间」。
  每次扫描临时打开、用完即关：长期持有卷句柄会挡住别人。

拿不到文件表时**一律抛错**，绝不返回空报告。空报告在界面上会显示成
"0 个碎片文件，卷很健康"，而事实是根本没看过——这类假象比报错危险得多。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from ..contract.models import Extent, FileFrag, VolumeInfo

__all__ = [
    "StaticCatalog",
    "JsonCatalog",
    "UnsupportedCatalog",
    "CatalogError",
    "open_ntfs_catalog",
]


class CatalogError(RuntimeError):
    """目录服务不可用或数据不完整。"""


class StaticCatalog:
    """内存目录。可绑定到某个具体卷，避免"拿演示布局去扫真实盘"。

    ``volume_key`` 是 ``VolumeInfo.device_path``。绑定的意义：目录服务与卷
    必须一一对应。否则用户在演示模式下切到 C: 再点扫描，会得到一份用
    合成数据算出来的"碎片报告"，看起来完全正常——这是最糟的一类错误。
    """

    def __init__(
        self,
        files: Iterable[FileFrag] = (),
        free: Iterable[Extent] = (),
        volume_key: str | None = None,
    ) -> None:
        self._files: list[FileFrag] = list(files)
        self._free: list[Extent] = list(free)
        self.volume_key = volume_key

    def set(self, files: Iterable[FileFrag], free: Iterable[Extent]) -> None:
        self._files = list(files)
        self._free = list(free)

    def _check(self, volume: VolumeInfo) -> None:
        if self.volume_key is not None and volume.device_path != self.volume_key:
            raise CatalogError(
                f"当前目录服务只包含卷 {self.volume_key} 的布局，"
                f"无法用于 {volume.mount_point}"
            )

    def list_files(self, volume: VolumeInfo) -> Iterable[FileFrag]:
        self._check(volume)
        return tuple(self._files)

    def free_extents(self, volume: VolumeInfo) -> Iterable[Extent]:
        self._check(volume)
        return tuple(self._free)


class JsonCatalog:
    """从 JSON 文件读布局。

    格式::

        {
          "files": [{"id": "f1", "path": "a.bin", "extents": [[0, 10], [40, 5]]}],
          "free":  [[10, 30], [45, 55]]
        }
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise CatalogError(f"布局文件不存在: {self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._files = tuple(
            FileFrag(
                file_id=str(item.get("id") or item["path"]),
                path=str(item["path"]),
                extents=tuple(Extent(int(s), int(n)) for s, n in item["extents"]),
            )
            for item in raw.get("files", ())
        )
        self._free = tuple(Extent(int(s), int(n)) for s, n in raw.get("free", ()))

    def list_files(self, volume: VolumeInfo) -> Iterable[FileFrag]:
        return self._files

    def free_extents(self, volume: VolumeInfo) -> Iterable[Extent]:
        return self._free


def open_ntfs_catalog(
    volume: VolumeInfo,
    *,
    min_clusters: int = 1,
    on_progress=None,
):
    r"""为真实卷打开文件目录服务。**这是真机扫描的入口。**

    两条路，自动选：

    1. **MFT 解析**（快，一次拿到全卷）——需要读 ``\\.\X:``，也就是管理员权限；
    2. **目录遍历 + 逐文件簇映射**（慢，不需要任何权限）——普通用户的唯一选择。

    以前只有第 1 条，于是未提权时点"扫描"必然失败；而扫描正是用户第一次点的
    那颗按钮。第 2 条虽然慢，但**能跑**，且给出的是真实数据（只是自由空间靠
    反推、打不开的文件看不到），所以它返回的目录服务带 ``restricted=True``，
    引擎据此只允许预览、不允许执行。

    句柄由调用方在用完后关闭（:meth:`lbamover.ntfs.MftCatalog.close`）。
    """
    if os.name != "nt":
        raise CatalogError("实时文件表解析只在 Windows 上可用")
    # 延迟导入：engine 包的其它部分在非 Windows 上也要能 import
    from ..devices import DeviceSpec, open_device
    from ..devices.base import DeviceError
    from ..ntfs import MftCatalog
    from ..platform.dirscan import DirectoryCatalog
    from ..platform.errors import DeviceUnavailable

    if not volume.filesystem.upper().startswith("NTFS"):
        # 非 NTFS 没有 $MFT，但目录遍历照样能拿到簇映射 —— 预览仍然有价值，
        # 只是执行路径本来就不支持（MOVE_FILE 是 NTFS 的）。
        return DirectoryCatalog(volume, on_progress=on_progress)

    try:
        device = open_device(
            DeviceSpec(
                kind="win32",
                path=volume.device_path,
                block_size=volume.geometry.block_size,
                writable=False,
                unbuffered=True,
            )
        )
    except (DeviceError, DeviceUnavailable) as exc:
        winerror = getattr(exc, "winerror", 0) or 0
        if winerror in (5, 32) or "拒绝访问" in str(exc) or "Access is denied" in str(exc):
            # 没提权：回落到目录遍历。这不是降级为"假数据"——文件→簇区间
            # 仍然来自文件系统本身，只是拿不到 $Bitmap、看不到打不开的文件。
            return DirectoryCatalog(volume, on_progress=on_progress)
        raise CatalogError(f"打开卷 {volume.device_path} 失败：{exc}") from exc
    return MftCatalog(device, min_clusters=min_clusters, root=volume.mount_point)


class UnsupportedCatalog:
    """真机上的占位实现：明确拒绝，不返回空结果。

    返回空结果会让 UI 显示"0 个碎片文件，卷很健康"，而事实是"我们根本没
    看过"。这两者相差一个会误导用户做出错误决策的假象。
    """

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or "本版未实现实时 NTFS 文件枚举（MFT / retrieval pointers）"

    def list_files(self, volume: VolumeInfo) -> Iterable[FileFrag]:
        raise CatalogError(self.reason)

    def free_extents(self, volume: VolumeInfo) -> Iterable[Extent]:
        raise CatalogError(self.reason)
