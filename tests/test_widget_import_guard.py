"""widget 层「不许 import engine」的静态守卫。

``ui/widgets/__init__.py`` 的文档里写着这条约定，但**约定没有守卫就只是注释**：
上一轮 ``params_panel.py`` 里真的出现过 ``from ...engine.scheduler import ...``，
靠人工 grep 才发现。widget 一旦依赖引擎，就再也不能用假数据单独渲染/截图/做
快照测试 —— 而这正是这一层存在的理由。

为什么用 ``ast`` 而不是正则：``__init__.py`` 第 3 行那句约定本身就含 ``engine``
这个词，注释、字符串、日志文案里也会出现。正则裸匹配会把它们统统报成违规，
守卫就会因为"误报太多"被人关掉 —— 不准确的守卫比没有守卫更糟。

覆盖的写法（相对导入的点数不同、绝对导入、只 import 到包一级都算）::

    from ...engine.scheduler import JobRunner      # widgets 里的模块上跳三级
    from ..engine.scheduler import JobRunner
    import lbamover.engine
    import lbamover.engine.scheduler as sched
    from lbamover.engine import scheduler
    from lbamover import engine                    # 名字挂在包上，不是模块名

允许的范围：**只扫 widgets 目录**。``ui/main_window.py`` 是装配层，它必须
import engine 才能把引擎接到界面上；把这层也毙掉等于要求界面不接引擎。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import lbamover

#: ``src/`` 根目录，用来把绝对路径换成"相对仓库"的可读路径
_SRC_ROOT = Path(lbamover.__file__).resolve().parent.parent
_UI_DIR = _SRC_ROOT / "lbamover" / "ui"
WIDGETS_DIR = _UI_DIR / "widgets"
MAIN_WINDOW = _UI_DIR / "main_window.py"

#: 被禁的模块名（按点分段比较，避免把 ``engine_utils`` 这类名字误判）
BANNED_SEGMENT = "engine"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(_SRC_ROOT.parent)).replace("\\", "/")
    except ValueError:  # pragma: no cover - 只有测试自己传入临时文件时才会走到
        return str(path)


def _package_parts(path: Path) -> tuple[str, ...]:
    """文件所在包的层级，例如 ``src/lbamover/ui/widgets/x.py`` → ``(lbamover, ui, widgets)``。

    相对导入要解析成绝对路径才知道它指向谁，而解析的起点就是这个包路径。
    """
    try:
        parts = path.resolve().relative_to(_SRC_ROOT).with_suffix("").parts
    except ValueError:  # pragma: no cover - 同上
        return ()
    return parts[:-1]


def _resolve_from(package: tuple[str, ...], level: int, module: str) -> tuple[str, ...]:
    """把 ``from ...engine.x import y`` 还原成绝对模块路径的各段。

    ``level`` 是前导点个数：1 指当前包，2 指上一层，以此类推。
    """
    tail = tuple(p for p in module.split(".") if p)
    if level == 0:
        return tail
    keep = len(package) - (level - 1)
    base = package[: max(0, keep)]
    return base + tail


def engine_offences(path: Path, source: str) -> list[str]:
    """返回 ``["文件:行号 → 语句", ...]``；空列表表示这个文件是干净的。

    暴露成公开函数是为了让"守卫本身被抓错过"这件事可测：如果它永远返回空，
    ``widgets`` 全绿也说明不了任何问题（见 ``TestGuardIsNotEmpty``）。
    """
    tree = ast.parse(source, filename=str(path))
    package = _package_parts(path)
    offences: list[str] = []

    def record(lineno: int, stmt: str) -> None:
        offences.append(f"{_display_path(path)}:{lineno} → {stmt}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if BANNED_SEGMENT in alias.name.split("."):
                    record(node.lineno, ast.unparse(node))
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from(package, node.level, node.module or "")
            hit = BANNED_SEGMENT in target
            if not hit:
                # ``from lbamover import engine``：engine 是导入的名字而不是模块段
                hit = any(alias.name == BANNED_SEGMENT for alias in node.names)
            if hit:
                record(node.lineno, ast.unparse(node))
    return offences


def widget_sources() -> list[Path]:
    """widgets 目录下的全部 .py（含子包，因为约定是"任何 widget"）。"""
    return sorted(WIDGETS_DIR.rglob("*.py"))


class TestWidgetsDoNotImportEngine:
    def test_no_widget_imports_engine(self):
        violations: list[str] = []
        for path in widget_sources():
            violations.extend(engine_offences(path, path.read_text(encoding="utf-8")))

        assert violations == [], (
            "widget 只许依赖 contract，禁止 import engine。"
            f"发现 {len(violations)} 处违规：\n  " + "\n  ".join(violations)
        )

    def test_scans_a_non_empty_set(self):
        """守卫必须有东西可扫，否则"全绿"是因为扫了个空目录。"""
        names = {p.name for p in widget_sources()}
        assert len(names) >= 5, f"widgets 目录下的模块少得可疑：{sorted(names)}"
        assert "params_panel.py" in names, "上一轮出问题的文件必须在扫描范围内"


class TestGuardIsNotEmpty:
    """守卫自身也要被抓错一次，否则它是"永远通过的测试"。"""

    @pytest.mark.parametrize(
        "snippet",
        [
            "from ...engine.scheduler import JobRunner",
            "from ..engine.scheduler import JobRunner",
            "from ...engine import scheduler",
            "import lbamover.engine",
            "import lbamover.engine.scheduler as sched",
            "from lbamover.engine import scheduler",
            "from lbamover.engine.scheduler import JobRunner",
            "from lbamover import engine",
            "def load():\n    from ...engine.executor import Executor\n    return Executor",
        ],
        ids=[
            "相对三级",
            "相对二级",
            "相对三级只到包",
            "绝对import包",
            "绝对import别名",
            "绝对from包",
            "绝对from子模块",
            "engine挂在包名下",
            "函数体内的延迟导入",
        ],
    )
    def test_catches_every_import_form(self, snippet):
        fake = WIDGETS_DIR / "_fake_widget.py"
        offences = engine_offences(fake, snippet + "\n")
        assert offences, f"守卫漏掉了这种写法：{snippet!r}"
        assert ":1" in offences[0] or ":2" in offences[0], offences

    def test_mentions_in_comments_and_strings_do_not_trigger(self):
        """注释与字符串里出现 engine 不算违规 —— 这正是不能用正则的原因。"""
        source = (
            '"""widgets 子包。\n\n'
            "约定：**任何 widget 都不 import engine**。\n"
            '"""\n'
            "\n"
            "# 以前这里写过 from ...engine.scheduler import JobRunner，已拆掉\n"
            'HINT = "不要 import engine，也不要 import lbamover.engine"\n'
            'MSG = "engine 相关的东西由装配层注入"\n'
        )
        fake = WIDGETS_DIR / "_fake_widget.py"
        assert engine_offences(fake, source) == []

    def test_contract_imports_are_fine(self):
        """契约层是允许依赖的方向，守卫不能把它也算进来。"""
        source = (
            "from ...contract.models import JobParams, MAX_IGNORED_FAILURES\n"
            "from lbamover.contract.enums import Strategy\n"
            "import lbamover.contract.models\n"
        )
        fake = WIDGETS_DIR / "_fake_widget.py"
        assert engine_offences(fake, source) == []


class TestAssemblyLayerIsOutOfScope:
    """装配层（main_window）允许 import engine，守卫的扫描范围必须排除它。"""

    def test_main_window_is_not_scanned(self):
        scanned = {p.name for p in widget_sources()}
        assert MAIN_WINDOW.name not in scanned, (
            "main_window.py 是装配层，它必须 import engine 才能把引擎接到界面；"
            "守卫只许扫 widgets/，否则会把它一起毙掉"
        )

    def test_main_window_really_imports_engine(self):
        """确认上面那条排除是**有意的**：它确实 import 了 engine。

        若哪天装配层不再 import engine，这条会红 —— 那时请连同上面的排除说明一起
        更新，而不是把这条删掉了事。
        """
        offences = engine_offences(
            MAIN_WINDOW, MAIN_WINDOW.read_text(encoding="utf-8")
        )
        assert offences, (
            "main_window.py 当前应当 import engine；守卫把它排除在扫描范围外是有意的"
        )
