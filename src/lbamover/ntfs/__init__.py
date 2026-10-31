"""NTFS 结构解析：从裸卷里读出「文件 → 簇区间」的映射。

存在的唯一理由：**没有文件表就没有可整理的单元**。引擎的规划器要的是
「某个文件占了哪几段簇」，这个信息只存在于 NTFS 自己的元数据里。拿不到
它，扫描只能返回一份空报告，而空报告会让用户以为"我的盘很健康"。

本包只做**只读解析**，一个字节都不写盘：

* :mod:`boot`    分区引导扇区 → 簇大小、$MFT 位置、记录大小
* :mod:`runlist` data run 解码（簇区间表）
* :mod:`record`  FILE 记录 + 属性解析（含 fixup 还原）
* :mod:`catalog` 串起来：遍历 $MFT、还原路径、导出 Extent 与自由空间
* :mod:`synth`   造一个结构合法的最小 NTFS 镜像，供测试与演练

单位约定：本包内部一切以**簇**为 LCN 单位、以**字节**为偏移单位。
转成引擎的 block 由 :class:`catalog.MftCatalog` 负责，且要求
``cluster_size`` 能被 ``block_size`` 整除。
"""

from __future__ import annotations

from .boot import NtfsBoot
from .catalog import CatalogStats, MftCatalog
from .record import FileRecord, NtfsAttribute
from .runlist import decode_runs

__all__ = [
    "CatalogStats",
    "FileRecord",
    "MftCatalog",
    "NtfsAttribute",
    "NtfsBoot",
    "decode_runs",
]
