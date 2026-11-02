"""强度档位的界面行为。

除了"点了能选上"，这里守三件在真机上会变成事故的事：

1. **五个按钮必须全部落在面板内**。QSS 的 padding 会把每个按钮的最小宽度
   撑到 81px，五个加起来正好跟面板可用宽度打平，DPI 一缩放第 5 个就被裁掉 ——
   用户看得见「彻底」却点不到「极端」，界面上没有任何提示。第一次实现
   就踩了这个坑。
2. **切模式要带出该模式的默认档**。``COMPACT`` 的旧语义是"全部文件都往
   卷首挤"，如果切过去仍是标准档（只动碎片文件），升级就成了一次静默的
   行为缩水。
3. **约束式任务不许显示可用的强度**。「腾出可用空间」必须搬开所有挡路的
   文件，留一个能点但无效的档位，用户会按"快速腾出"的结果去规划下一步。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from lbamover.contract.enums import Intensity, Strategy  # noqa: E402
from lbamover.ui.theme import DARK  # noqa: E402
from lbamover.ui.widgets.strategy_panel import (  # noqa: E402
    DEFAULT_INTENSITY,
    INTENSITY_ORDER,
    StrategyPanel,
)


@pytest.fixture()
def app():
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst


@pytest.fixture()
def panel(app):
    p = StrategyPanel(DARK)
    p.resize(320, 240)
    p.show()
    app.processEvents()
    return p


class TestIntensitySelection:
    def test_default_is_standard(self, panel):
        assert panel.intensity() is Intensity.STANDARD
        assert panel.strategy() is Strategy.COALESCE

    def test_click_emits(self, panel):
        seen: list[Intensity] = []
        panel.intensityChanged.connect(seen.append)
        panel._intensity_buttons[INTENSITY_ORDER.index(Intensity.EXTREME)].click()
        assert seen == [Intensity.EXTREME]
        assert panel.intensity() is Intensity.EXTREME

    def test_set_intensity_is_silent(self, panel):
        """程序化恢复状态不该触发"重算预演"——否则每次启动都会多算一遍。"""
        seen: list[Intensity] = []
        panel.intensityChanged.connect(seen.append)
        panel.set_intensity(Intensity.DEEP)
        assert panel.intensity() is Intensity.DEEP
        assert seen == []

    def test_hint_mentions_the_scope(self, panel):
        """说明行必须讲"动多少文件"，那是用户真正在选的东西。"""
        panel.set_intensity(Intensity.THOROUGH)
        assert "全部文件" in panel.intensity_hint.text()
        panel.set_intensity(Intensity.EXTREME)
        assert "大文件" in panel.intensity_hint.text()


class TestSwitchingModeCarriesDefaultIntensity:
    @pytest.mark.parametrize(
        "index,expected",
        [
            (1, DEFAULT_INTENSITY[Strategy.COALESCE]),  # 合并碎片
            (2, DEFAULT_INTENSITY[Strategy.COMPACT]),  # 向卷首聚拢
            (3, DEFAULT_INTENSITY[Strategy.RELOCATE_TAIL]),  # 搬到卷尾
        ],
    )
    def test_mode_click_sets_default(self, panel, index, expected):
        panel._group.button(index).click()
        assert panel.intensity() is expected

    def test_compact_defaults_to_thorough(self, panel):
        """``COMPACT`` 旧语义就是「全部文件」，默认档必须是彻底，不能是标准。"""
        assert DEFAULT_INTENSITY[Strategy.COMPACT] is Intensity.THOROUGH

    def test_user_choice_survives_until_mode_switch(self, panel):
        panel.set_intensity(Intensity.DEEP)
        assert panel.intensity() is Intensity.DEEP
        panel._group.button(1).click()  # 切回合并碎片 → 回到标准档
        assert panel.intensity() is Intensity.STANDARD


class TestIntensityDisabledForConstraintTasks:
    def test_reclaim_disables_row(self, panel):
        panel._group.button(4).click()
        assert not panel.intensity_row.isEnabled()
        assert "不适用" in panel.intensity_hint.text()
        assert "挡路" in panel.intensity_hint.text(), (
            "要说明**为什么**不适用，否则用户以为功能坏了"
        )

    def test_analyze_only_disables_row(self, panel):
        panel._group.button(0).click()
        assert not panel.intensity_row.isEnabled()
        assert "不适用" in panel.intensity_hint.text()

    def test_switching_back_reenables(self, panel):
        panel._group.button(4).click()
        panel._group.button(1).click()
        assert panel.intensity_row.isEnabled()


class TestExtremeThreshold:
    def test_row_hidden_except_extreme(self, panel):
        assert not panel.extreme_row.isVisibleTo(panel)
        panel.set_intensity(Intensity.EXTREME)
        panel._apply_intensity_visual(Intensity.EXTREME)
        assert panel.extreme_row.isVisibleTo(panel)

    def test_default_threshold(self, panel):
        assert panel.extreme_min_mib() == 256

    def test_change_emits(self, panel):
        seen: list[int] = []
        panel.extremeMinChanged.connect(lambda: seen.append(panel.extreme_min_mib()))
        panel.extreme_min.setValue(512)
        assert seen == [512]

    def test_programmatic_set_is_silent(self, panel):
        seen: list[int] = []
        panel.extremeMinChanged.connect(lambda: seen.append(1))
        panel.set_extreme_min_mib(1024)
        assert panel.extreme_min_mib() == 1024
        assert seen == []


class TestFiveButtonsFit:
    def test_last_button_inside_panel(self, panel):
        """第 5 个按钮必须落在面板内 —— 这是第一次实现时真实被裁掉的那个。

        只断言"按钮存在"是不够的：它存在、可点、``isVisible()`` 也是 True，
        只是画在面板外面看不见。所以要比较几何。
        """
        last = panel._intensity_buttons[-1]
        right = last.x() + last.width()
        assert right <= panel.width() + 1, (
            f"「{last.text()}」右边界 {right} 超出面板宽度 {panel.width()}，"
            "用户点不到这个档位"
        )

    def test_buttons_are_equal_width(self, panel):
        """等宽才像一条刻度；宽度随文字变会看不出这是从轻到重的谱。"""
        widths = {b.width() for b in panel._intensity_buttons}
        assert len(widths) <= 2, f"五档宽度不一致：{sorted(widths)}"
        assert max(widths) - min(widths) <= 2


class TestMainWindowWiring:
    def test_intensity_reaches_params(self, app, monkeypatch):
        from lbamover.ui import main_window as mw

        w = mw.MainWindow(palette=DARK)
        try:
            assert w._params.intensity is Intensity.STANDARD
            w.strategy_panel._intensity_buttons[4].click()
            assert w._params.intensity is Intensity.EXTREME
        finally:
            w.close()

    def test_extreme_threshold_reaches_params(self, app):
        from lbamover.ui import main_window as mw

        w = mw.MainWindow(palette=DARK)
        try:
            w.strategy_panel.set_intensity(Intensity.EXTREME)
            w.strategy_panel.extreme_min.setValue(768)
            assert w._params.extreme_min_mib == 768
        finally:
            w.close()

    def test_intensity_survives_param_panel_change(self, app):
        """改并发数会造一份全新的 JobParams —— 档位不能被悄悄重置成默认。

        这是 ``_merge_reclaim`` 存在的理由，也是它必须一起合并强度的理由：
        预演按极端档算出 4 万个文件、执行按标准档只动 200 个，用户完全看不出来。
        """
        from lbamover.contract.models import JobParams
        from lbamover.ui import main_window as mw

        w = mw.MainWindow(palette=DARK)
        try:
            w.strategy_panel._intensity_buttons[4].click()
            assert w._params.intensity is Intensity.EXTREME
            w._on_params(JobParams(concurrency=4))
            assert w._params.intensity is Intensity.EXTREME
        finally:
            w.close()
