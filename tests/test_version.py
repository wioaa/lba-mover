"""版本号一致性。

版本号散落在三处（pyproject 的 version / [tool.lbamover].revision、
``lbamover.__init__`` 的常量、exe 版本资源的生成逻辑），任何一处忘了改，
表现都是"对外宣称 2.0.0A，用户装出来是 2.0.0"这种查起来很烦的事。

这里不测"版本号等于多少"（那会每天都要改），只测**三处互相一致**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import lbamover

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject_project_version() -> str:
    """只读 [project] 段里的 version，避免被 [tool] 里的同名键带偏。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    section = text.split("[project]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
    assert m is not None, "pyproject.toml 的 [project] 段缺少 version"
    return m.group(1)


def _pyproject_revision() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    section = text.split("[tool.lbamover]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'^revision\s*=\s*"([^"]*)"', section, re.MULTILINE)
    assert m is not None, "pyproject.toml 缺少 [tool.lbamover].revision"
    return m.group(1)


class TestVersionConsistency:
    def test_three_segment_version_matches_pyproject(self):
        assert lbamover.VERSION == _pyproject_project_version()

    def test_revision_matches_pyproject(self):
        assert lbamover.REVISION == _pyproject_revision()

    def test_display_is_version_plus_revision(self):
        assert lbamover.VERSION_DISPLAY == f"{lbamover.VERSION}{lbamover.REVISION}"

    def test_version_is_pep440_clean(self):
        """三段必须能被 setuptools 接受；字母只能在 revision 里。"""
        assert re.fullmatch(r"\d+\.\d+\.\d+", lbamover.VERSION), lbamover.VERSION
        assert re.fullmatch(r"[A-Z]?", lbamover.REVISION), lbamover.REVISION

    def test_executable_version_resource_is_numeric(self):
        """exe 的 FileVersion 只能是数字，字母不能漏进去。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_version_file", ROOT / "packaging" / "make_version_file.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        numeric = mod.numeric_version()
        assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", numeric), numeric
        assert numeric.split(".")[:3] == lbamover.VERSION.split(".")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
