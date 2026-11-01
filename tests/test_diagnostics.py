"""失败诊断信息。

这一组用例守的是一件很具体的事：**用户能不能把问题说清楚**。

旧版只写一行 ``op12 失败：R:\\a.pst — 拒绝访问``。用户拿这句话去求助，
对方能问的只有"什么系统、什么版本、哪一步、错误码多少" —— 而这些都是程序
本来就知道的。所以这里逐项断言"该说的都说了"：

* 阶段（打开文件 / 搬簇 / 读写数据）—— 决定往哪个方向查；
* 系统错误码 —— 决定是人能处理（关掉占用程序）还是得修盘；
* 错误分类 —— 决定"要不要再点一次"；
* 建议 —— 直接给出下一步动作；
* 报告头 —— 版本 / 系统 / 卷 / 参数，缺一样都复现不了。
"""

from __future__ import annotations

import pytest

from lbamover.contract.models import FailureDetail, Geometry, JobParams, VolumeInfo
from lbamover.contract.enums import VolumeKind
from lbamover.diagnostics import (
    WINERROR_HINTS,
    build_report,
    format_failure,
    hint_for,
)


def _failure(**kw) -> FailureDetail:
    base = dict(
        path="R:\\data\\a.pst",
        op_id=12,
        stage="打开文件",
        reason="DeviceError: 拒绝访问",
        error_type="DeviceError",
        error_class="fatal",
        winerror=0,
    )
    base.update(kw)
    return FailureDetail(**base)


class TestHintFor:
    def test_known_winerror_gets_actionable_hint(self):
        hint = hint_for(_failure(winerror=32))
        assert "占用" in hint and "关掉" in hint

    def test_winerror_wins_over_stage(self):
        # 错误码比阶段更具体：同样是"打开文件"失败，5 和 32 的处理方式不同
        assert hint_for(_failure(winerror=5)) != hint_for(_failure(winerror=32))

    def test_unknown_code_falls_back_to_stage(self):
        hint = hint_for(_failure(winerror=9999, stage="搬簇"))
        assert "chkdsk" in hint

    def test_no_stage_no_code_still_says_something_useful(self):
        hint = hint_for(_failure(stage="", winerror=0))
        assert hint, "至少要说清楚下一步能做什么"

    def test_all_hints_are_non_empty(self):
        for code, hint in WINERROR_HINTS.items():
            assert hint.strip(), f"{code} 的建议是空的"

    def test_verify_mismatch_is_marked_retryable(self):
        hint = hint_for(_failure(error_class="verify_mismatch", stage=""))
        assert "再" in hint or "重试" in hint


class TestFormatFailure:
    def test_contains_every_field_a_helper_would_ask_for(self):
        text = format_failure(1, _failure(winerror=32))
        for needle in ("R:\\data\\a.pst", "阶段：打开文件", "原因：", "DeviceError",
                       "不可重试", "winerror=32", "建议："):
            assert needle in text, f"缺了 {needle}\n{text}"

    def test_stage_is_never_omitted(self):
        # 阶段未知也必须写出来："未知"本身也是信息（说明是兜底路径抛的）
        text = format_failure(1, _failure(stage=""))
        assert "阶段：" in text

    def test_hint_can_be_suppressed_for_dense_listing(self):
        text = format_failure(1, _failure(winerror=32), with_hint=False)
        assert "建议" not in text


class TestBuildReport:
    def _volume(self):
        return VolumeInfo(
            device_path=r"\\.\R:",
            mount_point="R:\\",
            label="data",
            filesystem="NTFS",
            kind=VolumeKind.FIXED,
            geometry=Geometry(block_size=4096, block_count=1024 * 1024),
            free_bytes=0,
            writable=True,
        )

    def test_report_has_environment_volume_and_params(self):
        text = build_report(
            volume=self._volume(),
            strategy="coalesce",
            scope="整卷",
            params=JobParams(chunk_blocks=256, concurrency=2, verify=True),
            moved_ops=19,
            failed_ops=1,
            failures=[_failure(winerror=32)],
        )
        assert "LBAMover" in text
        assert "目标卷: R:\\ [NTFS]" in text
        assert "策略: coalesce" in text
        assert "参数: chunk=256" in text
        assert "结果: 完成 19 / 失败 1" in text
        assert "winerror=32" in text
        assert "报告结束" in text

    def test_report_mentions_abort_reason(self):
        text = build_report(aborted="连续 20 个文件失败，判断为系统性故障，已中止剩余任务")
        assert "中止" in text and "系统性故障" in text

    def test_report_caps_failure_list_but_states_total(self):
        failures = [_failure(op_id=i, path=f"R:\\f{i}.bin") for i in range(30)]
        text = build_report(failed_ops=30, failures=failures, max_failures=5)
        assert "共 30 个" in text
        assert "另有 25 个" in text
        assert text.count("阶段：") == 5

    def test_report_includes_log_tail(self):
        text = build_report(log_tail=["第一行", "第二行"])
        assert "最近日志" in text and "第一行" in text

    def test_report_without_volume_still_works(self):
        """扫描都没跑起来就崩了，也得能出报告 —— 这正是最需要求助的时刻。"""
        text = build_report(strategy="", failures=[])
        assert "LBAMover" in text and "报告结束" in text
