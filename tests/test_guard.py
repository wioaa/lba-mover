"""T01 验收：执行期守卫 ``app/core/guard.py``（设计文档 §6 T01 验收标准 4）。

同样要求纯算法层，不碰真实磁盘。
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.guard import SafetyGuard  # noqa: E402
from app.core.models import BitmapSnapshot  # noqa: E402
from app.core.safety import SafetyPolicy  # noqa: E402


def _bitmap(total: int, occupied: list) -> BitmapSnapshot:
    bits = bytearray((total + 7) // 8)
    for lcn in occupied:
        bits[lcn >> 3] |= 1 << (lcn & 7)
    return BitmapSnapshot(bits=bytes(bits), total_clusters=total, bytes_per_cluster=4096)


def _policy(total: int = 1000) -> SafetyPolicy:
    return SafetyPolicy(
        total_clusters=total,
        bytes_per_cluster=4096,
        head_reserve_clusters=100,
        tail_reserve_clusters=100,
        mft_zone_start=400,
        mft_zone_end=500,
        mft_start_lcn=0,
        mft2_start_lcn=0,
        guard_metadata=False,
    )


def test_allow_ok_when_free() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    ok, reason = guard.allow(200, 10)
    assert ok and reason == ""
    assert guard.stats["allowed"] == 1


def test_allow_rejects_occupied() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, [205]))
    ok, reason = guard.allow(200, 10)
    assert not ok and "空闲" in reason
    assert guard.stats["rejected"] == 1


def test_allow_rejects_forbidden_zone() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    for lcn, clusters in ((50, 10), (450, 10), (890, 20), (95, 10)):
        ok, reason = guard.allow(lcn, clusters)
        assert not ok, (lcn, clusters)
        assert "保留区" in reason or "越" in reason


def test_allow_accepts_exact_boundaries() -> None:
    """恰好抵到禁区边界的区间必须放行（[100,110) 与 [880,900)）。"""
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    assert guard.allow(100, 10)[0] is True
    assert guard.allow(880, 20)[0] is True
    assert guard.allow(390, 10)[0] is True


def test_allow_rejects_out_of_bounds() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    assert guard.allow(-1, 4)[0] is False
    assert guard.allow(0, 0)[0] is False
    assert guard.allow(995, 10)[0] is False


def test_allow_rejects_overlap_with_reserved() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    assert guard.allow(200, 50)[0] is True
    guard.reserve(200, 50)
    ok, reason = guard.allow(240, 20)
    assert not ok and "重叠" in reason


def test_reserve_release_is_reversible() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []))
    base = guard.reserved
    guard.reserve(200, 50)
    assert guard.reserved_clusters == 50
    guard.reserve(300, 20)
    assert guard.reserved_clusters == 70
    guard.release(200, 50)
    assert guard.reserved == [(300, 320)]
    guard.release(300, 20)
    assert guard.reserved == base
    assert guard.stats["reserved_clusters"] == 0


def test_release_partial_interval() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []), reserved=[(200, 300)])
    guard.release(250, 20)
    assert guard.reserved == [(200, 250), (270, 300)]


def test_release_unknown_range_is_noop() -> None:
    guard = SafetyGuard(_policy(), _bitmap(1000, []), reserved=[(200, 210)])
    guard.release(500, 10)
    assert guard.reserved == [(200, 210)]


def test_no_snapshot_rejects() -> None:
    guard = SafetyGuard(_policy(), None)
    ok, reason = guard.allow(200, 10)
    assert not ok and "位图" in reason


def test_uninitialized_policy_rejects() -> None:
    guard = SafetyGuard(SafetyPolicy(), _bitmap(1000, []))
    ok, reason = guard.allow(10, 1)
    assert not ok and "未初始化" in reason


def test_batch_size_allowed_after_reserve() -> None:
    """65,536 簇批次（设计里的单次上限）校验要走快路径且结果正确。"""
    total = 200_000
    guard = SafetyGuard(
        SafetyPolicy(total_clusters=total, bytes_per_cluster=4096),
        _bitmap(total, []),
    )
    ok, _ = guard.allow(1000, 65_536)
    assert ok
    guard.reserve(1000, 65_536)
    assert guard.allow(1000, 65_536)[0] is False


def test_guard_fuzz_matches_slow_path() -> None:
    """随机位图 / 随机区间下，守卫结论与"逐簇朴素判断"必须一致。"""
    rng = random.Random(20261023)
    for _ in range(300):
        total = rng.randint(64, 4096)
        occupied = sorted(rng.sample(range(total), rng.randint(0, total // 4)))
        snapshot = _bitmap(total, occupied)
        policy = SafetyPolicy(
            total_clusters=total,
            bytes_per_cluster=4096,
            head_reserve_clusters=rng.choice([0, 1, 8, 40]),
            tail_reserve_clusters=rng.choice([0, 1, 8, 40]),
            mft_zone_start=0,
            mft_zone_end=0,
            guard_metadata=False,
        )
        guard = SafetyGuard(policy, snapshot)
        start = rng.randint(0, total)
        clusters = rng.randint(1, 64)
        ok, _ = guard.allow(start, clusters)
        expected = (
            clusters > 0
            and start >= 0
            and start + clusters <= total
            and policy.is_range_allowed(start, clusters)
            and snapshot.range_is_free(start, clusters)
        )
        assert ok == expected, (total, occupied, start, clusters, ok, expected)


# --------------------------------------------------------------------------- #
# T01 验收 6：位图字节级快路径
# --------------------------------------------------------------------------- #
def test_range_is_free_fast_matches_slow_path() -> None:
    """1000 组随机 ``(start, clusters, bits)`` 与逐簇版结果**完全一致**。"""
    rng = random.Random(20240921)
    for _ in range(1000):
        total = rng.randint(1, 4000)
        bits = bytes(rng.randrange(256) for _ in range((total + 7) // 8))
        snapshot = BitmapSnapshot(bits=bits, total_clusters=total, bytes_per_cluster=4096)
        start = rng.randrange(0, total)
        clusters = rng.randint(1, total - start)
        assert snapshot.range_is_free_fast(start, clusters) == snapshot.range_is_free(
            start, clusters
        ), f"不一致：start={start} clusters={clusters} total={total}"


def test_range_is_free_fast_rejects_out_of_range() -> None:
    snapshot = BitmapSnapshot(bits=bytes(4), total_clusters=32, bytes_per_cluster=4096)
    assert snapshot.range_is_free_fast(-1, 1) is False
    assert snapshot.range_is_free_fast(0, 0) is False
    assert snapshot.range_is_free_fast(30, 3) is False
    assert snapshot.range_is_free_fast(0, 32) is True


def test_range_is_free_fast_is_at_least_20x_faster() -> None:
    """65,536 簇的判定耗时不高于逐簇版的 1/20（设计 §6.1 的承诺）。"""
    total = 1 << 16
    snapshot = BitmapSnapshot(bits=bytes(total // 8), total_clusters=total,
                              bytes_per_cluster=4096)

    def measure(func, rounds: int = 5) -> float:
        started = time.perf_counter()
        for _ in range(rounds):
            assert func(0, total) is True
        return time.perf_counter() - started

    measure(snapshot.range_is_free_fast)       # 预热
    fast = measure(snapshot.range_is_free_fast)
    slow = measure(snapshot.range_is_free)
    assert fast * 20 <= slow, f"快路径 {fast:.6f}s 未达到逐簇版 {slow:.6f}s 的 1/20"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
