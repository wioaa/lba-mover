"""T01 验收：安全保留区模型（``app/core/safety.py``）。

覆盖设计文档 §6 T01 验收标准 1–3 + 7（模型未被破坏）。
纯算法层，不需要 QApplication / 真实磁盘。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import MB, VolumeInfo  # noqa: E402
from app.core.ranges import ceil_div, merge_runs, subtract_runs  # noqa: E402
from app.core.safety import (  # noqa: E402
    HEAD_RESERVE_MIN_BYTES,
    MFT_GUARD_CLUSTERS,
    TAIL_RESERVE_MIN_BYTES,
    SafetyPolicy,
    SafetyViolation,
    assert_target_allowed,
    build_policy,
    clamp_reserve_mb,
    default_head_mb,
    default_tail_mb,
    min_reserve_clusters,
    policy_from_dict,
    safety_bullets,
    subtract_forbidden,
)


def _info(
    total: int = 1_000_000,
    bpc: int = 4096,
    mft_zone: tuple = (100_000, 200_000),
    mft_start: int = 100_000,
    mft2_start: int = 500_000,
) -> VolumeInfo:
    return VolumeInfo(
        letter="T",
        root="T:\\",
        total_clusters=total,
        bytes_per_cluster=bpc,
        mft_zone_start=mft_zone[0],
        mft_zone_end=mft_zone[1],
        mft_start_lcn=mft_start,
        mft2_start_lcn=mft2_start,
        is_ntfs=True,
    )


# --------------------------------------------------------------------------- #
# 1) clamp_reserve_mb 硬下限
# --------------------------------------------------------------------------- #
def test_clamp_reserve_never_below_floor() -> None:
    """任意 bpc / total 组合下，夹紧结果换算回簇后必须 ≥ 硬下限。"""
    for bpc in (512, 4096, 65536):
        floor = min_reserve_clusters(bpc)
        for total in (100_000, 20_000_000):
            for requested_mb in (0, -5, 1, 16, 64, 10**9):
                mb = clamp_reserve_mb(requested_mb, bpc, total)
                clusters = mb * MB // bpc
                assert clusters >= floor, (
                    f"bpc={bpc} total={total} req={requested_mb} -> {mb} MB = {clusters} 簇"
                    f" < 下限 {floor}"
                )
                # 不得吃掉超过半个卷（MB 粒度向上取整会多出不到 1 MB，这里给出容差）
                assert clusters <= max(floor, total // 2) + (MB // bpc) + 1, (
                    bpc,
                    total,
                    requested_mb,
                    clusters,
                )


def test_clamp_reserve_zero_returns_floor() -> None:
    """传 0 必须返回下限对应的 MB，而不是 0。"""
    for bpc in (512, 4096, 65536):
        assert clamp_reserve_mb(0, bpc, 1_000_000) > 0
        clusters = clamp_reserve_mb(0, bpc, 1_000_000) * MB // bpc
        assert clusters >= min_reserve_clusters(bpc)
    # 簇大小非法时不产生保留（避免除零）
    assert clamp_reserve_mb(16, 0, 1_000_000) == 0


def test_min_reserve_matches_spec() -> None:
    """下限 = max(16 簇, 1 MiB)；两种口径在极端簇大小下都要正确。"""
    assert min_reserve_clusters(4096) == max(16, ceil_div(1 * MB, 4096))  # 256
    assert min_reserve_clusters(65536) == 16                              # 1 MiB / 64 KiB = 16
    assert min_reserve_clusters(512) == 2048                              # 1 MiB / 512
    assert HEAD_RESERVE_MIN_BYTES == TAIL_RESERVE_MIN_BYTES == MB


def test_default_reserve_mb() -> None:
    """默认值不得低于下限。"""
    for bpc in (512, 4096, 65536):
        assert default_head_mb(bpc) * MB // bpc >= min_reserve_clusters(bpc)
        assert default_tail_mb(bpc) * MB // bpc >= min_reserve_clusters(bpc, head=False)
    assert default_head_mb(4096) == 16


# --------------------------------------------------------------------------- #
# 2) forbidden_runs 六种形态
# --------------------------------------------------------------------------- #
def test_forbidden_head_only() -> None:
    policy = SafetyPolicy(total_clusters=1000, bytes_per_cluster=4096, head_reserve_clusters=100)
    assert policy.forbidden_runs() == [(0, 100)]


def test_forbidden_tail_only() -> None:
    policy = SafetyPolicy(total_clusters=1000, bytes_per_cluster=4096, tail_reserve_clusters=100)
    assert policy.forbidden_runs() == [(900, 1000)]


def test_forbidden_mft_zone_in_middle() -> None:
    policy = SafetyPolicy(
        total_clusters=1000,
        bytes_per_cluster=4096,
        mft_zone_start=400,
        mft_zone_end=600,
        mft_start_lcn=0,
        mft2_start_lcn=0,
        guard_metadata=False,
    )
    assert policy.forbidden_runs() == [(400, 600)]


def test_forbidden_merged_when_adjacent() -> None:
    """首/尾/MFT 三者相邻时要合并成更少的区间。"""
    policy = SafetyPolicy(
        total_clusters=1000,
        bytes_per_cluster=4096,
        head_reserve_clusters=100,
        tail_reserve_clusters=100,
        mft_zone_start=100,
        mft_zone_end=200,
        mft_start_lcn=0,
        mft2_start_lcn=0,
        guard_metadata=False,
    )
    # 首 0–100 + MFT 100–200 合并成 0–200
    assert policy.forbidden_runs() == [(0, 200), (900, 1000)]


def test_forbidden_mft_end_clipped() -> None:
    """``mft_end > total`` 时必须被裁剪到卷尾。"""
    policy = SafetyPolicy(
        total_clusters=1000,
        bytes_per_cluster=4096,
        mft_zone_start=900,
        mft_zone_end=5000,
        mft_start_lcn=0,
        mft2_start_lcn=0,
        guard_metadata=False,
    )
    assert policy.forbidden_runs() == [(900, 1000)]


def test_forbidden_empty_when_total_zero() -> None:
    assert SafetyPolicy().forbidden_runs() == []
    assert SafetyPolicy(total_clusters=0).allowed_clusters == 0


def test_forbidden_mft_mirror_guard_window() -> None:
    """$MFT / $MFTMirr 守卫窗各 8 簇。"""
    policy = SafetyPolicy(
        total_clusters=10_000,
        bytes_per_cluster=4096,
        mft_start_lcn=100,
        mft2_start_lcn=5000,
        mft_guard_clusters=MFT_GUARD_CLUSTERS,
    )
    runs = policy.forbidden_runs()
    assert (100, 108) in runs
    assert (5000, 5008) in runs


def test_allowed_clusters_and_runs() -> None:
    policy = SafetyPolicy(
        total_clusters=1000,
        bytes_per_cluster=4096,
        head_reserve_clusters=100,
        tail_reserve_clusters=100,
    )
    assert policy.allowed_clusters == 800
    assert policy.allowed_runs() == [(100, 900)]
    assert sum(e - s for s, e in policy.allowed_runs()) == policy.allowed_clusters


# --------------------------------------------------------------------------- #
# 3) assert_target_allowed 的 8 个边界用例
# --------------------------------------------------------------------------- #
def _policy() -> SafetyPolicy:
    return SafetyPolicy(
        total_clusters=1000,
        bytes_per_cluster=4096,
        head_reserve_clusters=100,
        tail_reserve_clusters=100,
        mft_zone_start=400,
        mft_zone_end=500,
        mft_start_lcn=0,
        mft2_start_lcn=0,
        guard_metadata=False,
    )


def test_assert_rejects_negative_lcn() -> None:
    _expect_violation(-1, 10)


def test_assert_rejects_zero_clusters() -> None:
    _expect_violation(200, 0)


def test_assert_rejects_past_volume_end() -> None:
    _expect_violation(990, 20)


def test_assert_rejects_head_reserve() -> None:
    _expect_violation(50, 10)
    _expect_violation(0, 100)


def test_assert_rejects_tail_reserve() -> None:
    """跨入卷尾保留区必须拒绝；**恰好抵到边界**必须放行（off-by-one 回归）。"""
    _expect_violation(890, 20)   # 890..910 跨过 900
    _expect_violation(895, 10)   # 895..905 跨过 900
    assert_target_allowed(_policy(), 890, 10, "规划")   # 890..900 恰好抵边界
    assert_target_allowed(_policy(), 895, 5, "规划")    # 895..900 恰好抵边界


def test_assert_rejects_straddling_head_boundary() -> None:
    """跨越卷首保留区边界（90..110）。"""
    _expect_violation(90, 20)


def test_assert_rejects_mft_zone_overlap() -> None:
    _expect_violation(390, 20)
    _expect_violation(480, 30)


def test_assert_accepts_valid_targets() -> None:
    policy = _policy()
    for lcn, clusters in ((100, 10), (200, 100), (390, 10), (500, 100), (880, 20)):
        assert_target_allowed(policy, lcn, clusters, "规划")


def test_assert_rejects_uninitialized_policy() -> None:
    try:
        assert_target_allowed(SafetyPolicy(), 0, 1, "规划")
    except SafetyViolation:
        return
    raise AssertionError("应当抛 SafetyViolation")


def _expect_violation(lcn: int, clusters: int) -> None:
    try:
        assert_target_allowed(_policy(), lcn, clusters, "规划")
    except SafetyViolation as exc:
        assert exc.lcn == lcn and exc.clusters == clusters
        return
    raise AssertionError(f"({lcn}, {clusters}) 应当抛 SafetyViolation")


# --------------------------------------------------------------------------- #
# build_policy / describe / bullets / 序列化
# --------------------------------------------------------------------------- #
def test_build_policy_clamps_and_protects() -> None:
    info = _info()
    policy = build_policy(info, head_mb=0, tail_mb=0)
    assert policy.head_reserve_clusters >= min_reserve_clusters(info.bytes_per_cluster)
    assert policy.tail_reserve_clusters >= min_reserve_clusters(info.bytes_per_cluster, head=False)
    assert policy.mft_zone_start == info.mft_zone_start
    assert policy.protect_mft_zone is True
    assert policy.guard_metadata is True
    assert "卷首保留" in policy.describe()


def test_build_policy_degenerate_volume() -> None:
    """总簇数为 0 时不得崩溃，也不得产出非法禁区。"""
    policy = build_policy(VolumeInfo(), head_mb=16, tail_mb=16)
    assert policy.total_clusters == 0
    assert policy.forbidden_runs() == []


def test_build_policy_head_tail_never_eat_volume() -> None:
    """极端参数下 head+tail 不得大于总簇数。"""
    info = _info(total=1000, bpc=4096)
    policy = build_policy(info, head_mb=10**6, tail_mb=10**6)
    assert policy.head_reserve_clusters + policy.tail_reserve_clusters <= 1000


def test_safety_bullets_include_evidence() -> None:
    info = _info()
    bullets = safety_bullets(info, start_offset=104_857_600)
    assert len(bullets) >= 5
    joined = "".join(bullets)
    assert "104.9 MB" in joined or "104,857,600" in joined or "偏移" in joined
    bullets_zero = safety_bullets(info, start_offset=0)
    assert any("偏移 0" in b for b in bullets_zero)
    bullets_unknown = safety_bullets(info, start_offset=-1)
    assert all("实测" not in b for b in bullets_unknown)


def test_policy_roundtrip() -> None:
    policy = build_policy(_info(), head_mb=16, tail_mb=16)
    restored = policy_from_dict(policy.as_dict())
    assert restored.head_reserve_clusters == policy.head_reserve_clusters
    assert restored.tail_reserve_clusters == policy.tail_reserve_clusters
    assert restored.forbidden_runs() == policy.forbidden_runs()


def test_subtract_forbidden_helper() -> None:
    runs = [(120, 200), (300, 400)]
    forbidden = [(150, 160), (390, 410)]
    assert subtract_forbidden(runs, forbidden) == [(120, 150), (160, 200), (300, 390)]


# --------------------------------------------------------------------------- #
# 7) v1 模型未被破坏
# --------------------------------------------------------------------------- #
def test_models_backwards_compatible() -> None:
    """``VolumeInfo()`` 零参构造仍成立，v1 字段名与语义不变。"""
    info = VolumeInfo()
    assert info.letter == "" and info.total_clusters == 0
    assert info.mft2_start_lcn == 0 and info.bytes_per_file_record == 0
    assert info.media_kind == "unknown" and info.disk_start_offset == -1
    assert info.display_name is not None


def test_ranges_utils() -> None:
    assert merge_runs([(5, 10), (0, 3), (8, 12)]) == [(0, 3), (5, 12)]
    assert merge_runs([]) == []
    assert subtract_runs([(0, 10)], [(2, 4)]) == [(0, 2), (4, 10)]
    assert subtract_runs([(0, 10)], []) == [(0, 10)]
    assert subtract_runs([(0, 10)], [(0, 10)]) == []


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
