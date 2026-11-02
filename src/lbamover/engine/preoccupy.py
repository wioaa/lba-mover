"""目标区预占：搬之前先把目标区用占位文件扣住。

## 它防的是什么

规划时目标区是空闲的，执行时却可能被别的程序写进去了 —— 那一刻
``FSCTL_MOVE_FILE`` 会因为目标非空而失败。真机报告里那 3 个"布局复核不通过"
**不是**这类问题（那是源文件被改，由半锁定覆盖），所以这个功能防的是一种
尚未在现场观测到、但确实存在的风险。

## 它防不住什么

防不住规划阶段的抢占：占位只能在"计划已定、还没开始搬"的窗口里建立。
窗口两端都比它长得多，所以它把风险从"几分钟"压缩到"毫秒级"，**不是消除**。

## 为什么按 op 建占位，而不是建一个大的

``FSCTL_MOVE_FILE`` 要求目标区**完全空闲**。如果用一个占位文件覆盖整个计划的
目标区，那么执行第一个 op 之前必须先释放它 —— 释放一次就是全部放开，后面的
op 又裸露了。按 op 建，"释放这一段"和"立刻搬进这一段"之间只隔一次系统调用。

## 无法验证的部分（必须写在代码里）

真机路径需要 ``SeManageVolumePrivilege`` 与一块可写的 NTFS 卷，本机不具备。
所以这里只实现**调度逻辑**（占哪些区间、什么时候释放、失败怎么回滚），
Win32 那两个原语由 :class:`Win32ClusterMover` 提供，用假后端测试调用序列。
真机上第一次开启它时，要先拿镜像演练再上盘。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..contract.models import Extent, MoveOp

__all__ = ["PreoccupyGuard", "PreoccupyReport"]


@dataclass
class PreoccupyReport:
    """预占结果。失败要能说清是哪一个、为什么 —— 静默降级会让人以为受保护了。"""

    requested: int = 0
    occupied: int = 0
    blocks: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return not self.failures and self.occupied == self.requested

    def summary(self) -> str:
        if self.requested == 0:
            return "没有需要预占的目标区"
        if self.all_ok:
            return f"已占住 {self.occupied} 个目标区（{self.blocks} 块）"
        if self.occupied == 0:
            return f"目标区预占未生效：{self.failures[0] if self.failures else '未知原因'}"
        return (
            f"目标区只占住了 {self.occupied}/{self.requested} 个"
            f"（{self.failures[0] if self.failures else ''}）"
        )


class PreoccupyGuard:
    """管理一批占位文件的生命周期。

    占位文件必须落在**目标卷自己**上 —— ``FSCTL_MOVE_FILE`` 只在同一个卷内
    搬簇，跨卷的东西根本搬不过去。所以工作目录不是"随便一个临时目录"，而是
    卷根。
    """

    def __init__(self, mover, volume_root: str, cluster_size: int) -> None:
        self.mover = mover
        self.volume_root = volume_root
        self.cluster_size = max(1, cluster_size)
        self._paths: dict[int, str] = {}
        self._handles: list[object] = []
        self.report = PreoccupyReport()

    @property
    def supported(self) -> bool:
        """后端没有这两个原语时静默跳过 —— 那不是"失败"，是这套后端没有这能力。"""
        return hasattr(self.mover, "create_placeholder") and hasattr(
            self.mover, "delete_file"
        )

    # -- 生命周期 -------------------------------------------------------

    def occupy(self, ops: Sequence[MoveOp]) -> PreoccupyReport:
        """为每个 op 的目标区建占位。任一步失败就整体回滚。

        整体回滚而不是"能占几个算几个"：占了一半的计划在执行时仍然是不可靠的，
        而用户看到"已占住 12/30"会以为剩下的没事。要么全占，要么明确告诉他
        这个功能没生效、按原样跑。
        """
        self.report = PreoccupyReport(requested=len(ops))
        if not self.supported:
            self.report.failures.append("当前搬运后端不支持创建占位文件")
            return self.report
        if not ops:
            return self.report

        for op in ops:
            blocks = sum(e.length for e in op.src_extents)
            if blocks <= 0:
                continue
            path = self._path(op)
            try:
                handle = self.mover.create_placeholder(path, blocks * self.cluster_size)
                # 把占位文件的簇搬到目标区：这一步之后，那段空间对别的进程
                # 就是"已被占用"，写不进来。
                self.mover.move(handle, 0, op.dst_start, blocks)
                self.mover.close_file(handle)
                self._paths[op.op_id] = path
                self.report.occupied += 1
                self.report.blocks += blocks
            except Exception as exc:  # noqa: BLE001 - 占位是增强，失败要降级不要炸
                self.report.failures.append(f"op{op.op_id} 占位失败：{exc}")
                self.release_all()
                return self.report
        return self.report

    def release_for(self, op_id: int) -> None:
        """释放某个 op 的目标区。执行器在搬这个 op **之前**调用。"""
        path = self._paths.pop(op_id, None)
        if path is None:
            return
        try:
            self.mover.delete_file(path)
        except Exception:  # noqa: BLE001
            # 删不掉不是搬运失败：目标区可能仍被占着，MOVE_FILE 会报错并
            # 归入该 op 的失败明细 —— 那里有更准确的错误码，这里不该盖掉它。
            pass

    def release_all(self) -> None:
        for op_id in list(self._paths):
            self.release_for(op_id)

    def close(self) -> None:
        self.release_all()
        for h in self._handles:
            try:
                self.mover.close_file(h)
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()

    # -- 内部 -----------------------------------------------------------

    def _path(self, op: MoveOp) -> str:
        name = f".lbamover-placeholder-{op.op_id:06d}.tmp"
        return os.path.join(self.volume_root, name)
