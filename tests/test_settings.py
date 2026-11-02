"""配置持久化：写在哪、什么时候写、坏了怎么办。

这组用例守四件事，每条都对应一个真实的失败模式：

1. **往返一致**。少存一个字段的表现是"重开之后某一项悄悄回默认"，而界面
   上没有任何提示 —— 用户只会觉得"设置没生效"。
2. **原子写**。直接覆写时断电会得到半截 JSON，下次启动只能当损坏处理，
   等于设置全丢。所以写盘必须是"临时文件 + 替换"。
3. **坏了不能挡住启动**。配置是便利功能：语法错、字段越界、目录不可写，
   都只该在日志里说一声。
4. **保存时机**。改动后防抖 + 关窗强制落盘；只有后者会在崩溃时丢设置，
   只有前者会让拖动滑块时疯狂写盘。
"""

from __future__ import annotations

import json

import pytest

from lbamover.contract.enums import ExecMode, FreeSpaceSide, Intensity, Strategy
from lbamover.contract.models import JobParams
from lbamover.settings import (
    SCHEMA_VERSION,
    AppSettings,
    SettingsStore,
    default_config_path,
)


class TestPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.json"
        monkeypatch.setenv("LBAMOVER_CONFIG", str(target))
        assert default_config_path() == target

    def test_default_is_outside_the_repo(self, monkeypatch):
        """配置不能写进仓库目录：``git clean`` 会连同设置一起清掉。"""
        monkeypatch.delenv("LBAMOVER_CONFIG", raising=False)
        path = default_config_path()
        assert path.name == "settings.json"
        assert ".lbamover" not in str(path)


class TestRoundtrip:
    def test_every_field_survives(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.json")
        original = AppSettings(
            strategy=Strategy.PACK_TAIL,
            params=JobParams(
                exec_mode=ExecMode.MEMCACHED,
                mem_cache_blocks=16,
                mem_cache_block_kib=512,
                intensity=Intensity.EXTREME,
                extreme_min_mib=128,
                reclaim_blocks=4096,
                reclaim_at=FreeSpaceSide.HEAD,
                pack_span_blocks=8192,
                ignore_errors=True,
                partial_lock=True,
                verify=False,
                chunk_blocks=128,
                concurrency=4,
            ),
            volume_path="\\\\.\\F:",
            left_panel_visible=False,
            window_width=1500,
            window_height=980,
        )
        assert store.save(original) == ""
        loaded = SettingsStore(tmp_path / "settings.json").load()
        assert loaded.settings == original, "往返必须完全一致（含枚举与布尔）"

    def test_written_file_is_readable_json(self, tmp_path):
        path = tmp_path / "settings.json"
        SettingsStore(path).save(AppSettings())
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema"] == SCHEMA_VERSION
        assert "saved_at" in raw, "写文件的时间要能看出来（排查「设置没生效」用）"
        assert raw["params"]["exec_mode"] == "native", "枚举存 .value 才能跨版本读"


class TestMissingAndBroken:
    def test_first_run_is_not_an_error(self, tmp_path):
        loaded = SettingsStore(tmp_path / "nope.json").load()
        assert loaded.missing and not loaded.recovered and loaded.notes == []
        assert loaded.settings == AppSettings()

    def test_corrupt_json_is_quarantined(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{ not json", encoding="utf-8")
        loaded = SettingsStore(path).load()
        assert loaded.recovered
        assert loaded.settings == AppSettings()
        assert not path.exists(), "坏文件要被挪走，否则下次启动还是坏的"
        backups = list(tmp_path.glob("settings.json.broken-*"))
        assert backups, "必须留证：删掉就再也查不出为什么设置没生效"
        assert "备份" in loaded.notes[0]

    def test_bad_field_only_loses_that_field(self, tmp_path):
        """一个数字越界不该把整份参数清空 —— 用户改过的十几个选项一起消失。"""
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"schema": 1, "params": {"chunk_blocks": 99999, "verify": False}}),
            encoding="utf-8",
        )
        loaded = SettingsStore(path).load()
        assert loaded.settings.params.chunk_blocks == JobParams().chunk_blocks
        assert loaded.settings.params.verify is False, "合法的字段必须保住"
        assert any("chunk_blocks" in n for n in loaded.notes), "要点名是哪个字段"

    def test_unknown_enum_falls_back(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"schema": 1, "strategy": "no_such_mode", "params": {}}),
            encoding="utf-8",
        )
        loaded = SettingsStore(path).load()
        assert loaded.settings.strategy is Strategy.COALESCE
        assert any("no_such_mode" in n for n in loaded.notes)

    def test_future_schema_still_loads_known_fields(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"schema": SCHEMA_VERSION + 5, "strategy": "compact", "future": 1}),
            encoding="utf-8",
        )
        loaded = SettingsStore(path).load()
        assert loaded.settings.strategy is Strategy.COMPACT
        assert any("更新的版本" in n for n in loaded.notes)

    def test_cached_mode_restores_cache_dir_first(self, tmp_path):
        """``exec_mode`` 依赖 ``cache_dir``：应用顺序错了就会静默退回盘内搬运。"""
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {"schema": 1, "params": {"exec_mode": "cached", "cache_dir": "I:/cache"}}
            ),
            encoding="utf-8",
        )
        loaded = SettingsStore(path).load()
        assert loaded.settings.params.exec_mode is ExecMode.CACHED
        assert loaded.settings.params.cache_dir == "I:/cache"
        assert loaded.notes == []


class TestWrite:
    def test_atomic_no_temp_left_behind(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SettingsStore(path)
        assert store.save(AppSettings()) == ""
        assert store.save(AppSettings()) == ""
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"临时文件必须清理干净，实际留下 {leftovers}"

    def test_overwrites_previous_content_completely(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SettingsStore(path)
        store.save(AppSettings(strategy=Strategy.PACK_TAIL, volume_path="A" * 500))
        store.save(AppSettings(strategy=Strategy.COALESCE))
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["strategy"] == "coalesce"
        assert raw["volume_path"] == "", "旧的更长内容不能残留在文件尾部"

    def test_unwritable_target_reports_instead_of_raising(self, tmp_path):
        # 目标路径的父级是个文件 → mkdir 必然失败
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        store = SettingsStore(blocker / "sub" / "settings.json")
        error = store.save(AppSettings())
        assert error and "配置" in error
        assert store.last_error == error

    def test_clear_is_idempotent(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SettingsStore(path)
        store.save(AppSettings())
        assert store.clear() == ""
        assert store.clear() == ""


class TestUiIntegration:
    """主窗口的接线：恢复、防抖、关窗落盘。

    这些行为在 pytest 里只能靠 offscreen 窗口验证 —— 而它们恰恰是最容易
    "看起来实现了、实际没接上"的部分（信号连错、时序不对）。
    """

    @pytest.fixture()
    def qapp(self):
        QtWidgets = pytest.importorskip("PySide6.QtWidgets")
        return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _window(self, config_path, monkeypatch):
        monkeypatch.setenv("LBAMOVER_CONFIG", str(config_path))
        from lbamover.ui.main_window import MainWindow
        from lbamover.ui.theme import DARK

        return MainWindow(palette=DARK)

    def test_restore_applies_saved_state(self, qapp, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        SettingsStore(path).save(
            AppSettings(
                strategy=Strategy.RELOCATE_TAIL,
                params=JobParams(
                    intensity=Intensity.DEEP,
                    chunk_blocks=128,
                    exec_mode=ExecMode.MEMCACHED,
                    mem_cache_blocks=8,
                ),
                window_width=1200,
                window_height=900,
            )
        )
        w = self._window(path, monkeypatch)
        try:
            assert w._strategy is Strategy.RELOCATE_TAIL
            assert w.strategy_panel.strategy() is Strategy.RELOCATE_TAIL
            assert w._params.chunk_blocks == 128
            assert w.params_panel.chunk.value() == 128
            assert w.strategy_panel.intensity() is Intensity.DEEP
            assert w.params_panel.exec_mode() is ExecMode.MEMCACHED
            assert w.params_panel.mem_blocks.value() == 8
        finally:
            w.close()

    def test_changing_strategy_schedules_save(self, qapp, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        w = self._window(path, monkeypatch)
        try:
            assert not path.exists(), "还没改东西，不该写文件"
            w.strategy_panel._group.button(2).click()  # 向卷首聚拢
            assert w._save_timer.isActive(), "改动后应启动防抖计时器，而不是立刻写盘"
        finally:
            w.close()

    def test_close_flushes_even_if_timer_pending(self, qapp, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        w = self._window(path, monkeypatch)
        w.strategy_panel._group.button(2).click()
        assert w._save_timer.isActive()
        w.close()  # 计时器还没到点，但关窗必须强制落盘
        assert path.exists(), "关窗时必须强制保存，否则防抖窗口内的改动会丢"
        loaded = SettingsStore(path).load()
        assert loaded.settings.strategy is Strategy.COMPACT

    def test_corrupt_file_does_not_block_startup(self, qapp, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text("<<<", encoding="utf-8")
        w = self._window(path, monkeypatch)  # 不抛即通过
        try:
            assert w._strategy is Strategy.COALESCE
            assert any("配置" in line for line in w.log_view.plain_tail(20))
        finally:
            w.close()
