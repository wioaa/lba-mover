"""执行期安全守卫（**纯算法层**，无 Qt / 无 ctypes / 无 IO）。

``SafetyGuard`` 是「第二道校验」：mover 在**每次** ``FSCTL_MOVE_FILE`` 之前调用
:meth:`SafetyGuard.allow`，只有 ``ok=True`` 才允许写。这样即便规划之后卷的位图
发生变化、或计划本身有 bug，也不会写到不该写的簇上。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.models import BitmapSnapshot
from app.core.ranges import merge_runs
from app.core.safety import SafetyPolicy


class SafetyGuard:
    """把 ``SafetyPolicy`` + 位图快照 + 本次已占用目标区合成一个可判定的守卫。

    校验条件（全部满足才允许写）：

    1. ``clusters > 0``；
    2. 卷内：``lcn >= 0`` 且 ``lcn + clusters <= policy.total_clusters``；
    3. 不越禁区（卷首 / 卷尾 / MFT 区 / MFT 守卫窗）；
    4. 不与本次已成功写入的目标区重叠；
    5. 位图显示该区间空闲（``range_is_free_fast``）。
    """

    def __init__(
        self,
        policy: SafetyPolicy,
        snapshot: Optional[BitmapSnapshot],
        reserved: Iterable[Tuple[int, int]] = (),
    ) -> None:
        self.policy = policy
        self.snapshot = snapshot
        self._reserved: List[Tuple[int, int]] = merge_runs(list(reserved))
        self._allowed = 0
        self._rejected = 0
        self._last_reason = ""

    # ---------------- 判定 ---------------- #
    def refresh(self, snapshot) -> None:
        """换入新的位图视图（执行期影子位图 / 重读快照）。

        ``snapshot`` 只需提供 ``range_is_free_fast(start, clusters) -> bool``：
        :class:`app.core.models.BitmapSnapshot` 与
        :class:`app.core.shadow.ShadowBitmap` 都满足这一契约，所以守卫不必知道
        自己看到的是"一次性快照"还是"会随搬运推进而变化的影子位图"。

        ★ 这是 FIX-1 的落点：执行期每段之前都必须让守卫看到最新位图，
        否则驱逐腾出来的空间在静态快照里仍是"被占用" → 目标被误拒 →
        依赖驱逐的计划静默搬动 0 簇。
        """
        if snapshot is None:
            return
        self.snapshot = snapshot

    def allow(self, lcn: int, clusters: int) -> Tuple[bool, str]:
        """返回 ``(是否允许, 中文原因)``。拒绝时**不产生任何副作用**。"""
        ok, reason = self._check(lcn, clusters)
        if ok:
            self._allowed += 1
        else:
            self._rejected += 1
            self._last_reason = reason
        return ok, reason

    def _check(self, lcn: int, clusters: int) -> Tuple[bool, str]:
        if clusters <= 0:
            return False, "目标簇数必须大于 0"
        if lcn < 0:
            return False, "目标起始 LCN 不能为负"
        total = self.policy.total_clusters
        if total <= 0:
            return False, "安全策略未初始化（卷总簇数为 0）"
        if lcn + clusters > total:
            return False, (
                f"目标区间越过卷尾（{lcn + clusters:,} > {total:,}）"
            )
        if not self.policy.is_range_allowed(lcn, clusters):
            return False, "目标区间与安全保留区（卷首/卷尾/MFT）相交"

        end = lcn + clusters
        for start, reserved_end in self._reserved:
            if start < end and lcn < reserved_end:
                return False, (
                    f"目标区间与本次已占用的目标区 [{start:,}, {reserved_end:,}) 重叠"
                )

        if self.snapshot is None:
            return False, "缺少位图快照，无法确认目标是否空闲"
        if not self.snapshot.range_is_free_fast(lcn, clusters):
            return False, "位图显示目标区间并非全部空闲"

        return True, ""

    # ---------------- 登记 ---------------- #
    def reserve(self, lcn: int, clusters: int) -> None:
        """登记一段已写入的目标区（逐段成功后立即调用）。"""
        if clusters <= 0:
            return
        self._reserved = merge_runs(self._reserved + [(lcn, lcn + clusters)])

    def release(self, lcn: int, clusters: int) -> None:
        """回滚时把目标区从"已占用"里移除。"""
        if clusters <= 0:
            return
        target = (lcn, lcn + clusters)
        kept: List[Tuple[int, int]] = []
        for start, end in self._reserved:
            if end <= target[0] or start >= target[1]:
                kept.append((start, end))
                continue
            if start < target[0]:
                kept.append((start, target[0]))
            if end > target[1]:
                kept.append((target[1], end))
        self._reserved = kept

    # ---------------- 状态 ---------------- #
    @property
    def reserved(self) -> List[Tuple[int, int]]:
        return list(self._reserved)

    @property
    def reserved_clusters(self) -> int:
        return sum(e - s for s, e in self._reserved)

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "allowed": self._allowed,
            "rejected": self._rejected,
            "reserved_clusters": self.reserved_clusters,
        }

    @property
    def last_reason(self) -> str:
        return self._last_reason
