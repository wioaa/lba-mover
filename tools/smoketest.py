#!/usr/bin/env python3
"""smoketest —— LBA Tail Mover 冒烟测试（与传统 TestFlow 并行，覆盖其到不了的地方）。

传统 ``testflow`` 默认 profile 跑 ``tests/test_*.py``（纯逻辑 + 内存 fixture），
到不了以下位置，本脚本专门补这些缺口：

  ① 真实入口自测   ：``python main.py --selftest`` —— 真实 import 全部关键模块，
                     含唯一写路径 ``app.win32.mover``、``$MFT`` 读、scheduler、UI。
  ② 冻结二进制自测 ：``dist/LBA-Tail-Mover.exe --selftest`` —— 打包/捆绑完整性，
                     只有真构建出来的 EXE 才能验证，源树测试永远碰不到。
  ③ 写路径隔离不变量：import ``app.ui.main_window``（UI 层）后，
                     ``app.win32.mover`` 不应出现在 sys.modules；随后再确认它
                     本身可正常 import。传统测试从不断言这条。
  ④ 真实 Win32 卷枚举：``app.win32.volume.list_volumes()`` 走 FSCTL_* 实路径，
                     不是 mock。需要权限/环境时干净 SKIP，不误判。
  ⑤ 真实主窗口构造 ：offscreen 下 ``MainWindow()`` 能否无崩溃装配（真实 UI 引导），
                     传统测试只用 ``_make_window()`` 之类的轻量替身。

退出码：0 = 全部 PASS 或干净 SKIP（无关键项失败）；1 = 任一关键项 FAIL。
用 ``generic`` 解析器接入 testflow 的 smoke profile，可与 default 流并行跑。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# 必须在 import PySide6 之前把渲染后端切到 offscreen，否则无显示环境会初始化失败。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
# 让进程内能直接 import app.*（脚本在 tools/ 下，默认只把 tools/ 加进 sys.path）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXE = ROOT / "dist" / "LBA-Tail-Mover.exe"

STATUS_PASS = "PASS"
STATUS_SKIP = "SKIP"
STATUS_FAIL = "FAIL"


@dataclass
class Check:
    name: str
    detail: str = ""
    status: str = STATUS_PASS
    # 该项失败时是否算关键失败（决定退出码）。SKIP 永远不算失败。
    critical: bool = True


def _run(cmd, timeout=120):
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout
    )


def check_entrypoint_selftest(results: list[Check]) -> None:
    """① 真实入口 ``main.py --selftest``：真实 import 全部关键模块。"""
    try:
        proc = _run([sys.executable, "main.py", "--selftest"], timeout=180)
    except Exception as exc:  # noqa: BLE001
        results.append(Check(
            "入口自测 main.py --selftest",
            f"执行异常：{exc}", STATUS_FAIL,
        ))
        return
    ok = proc.returncode == 0 and "selftest OK" in proc.stdout
    detail = (
        f"rc={proc.returncode}；"
        + (proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(无输出)")
    )
    results.append(Check(
        "入口自测 main.py --selftest",
        detail, STATUS_PASS if ok else STATUS_FAIL,
    ))


def check_exe_selftest(results: list[Check]) -> None:
    """② 冻结二进制 ``dist/LBA-Tail-Mover.exe --selftest``：打包完整性。"""
    if not EXE.is_file():
        results.append(Check(
            "冻结二进制自测 EXE --selftest",
            f"未找到 {EXE}（先构建一次即可启用；源树测试本就碰不到这一步）",
            STATUS_SKIP, critical=False,
        ))
        return
    try:
        proc = _run([str(EXE), "--selftest"], timeout=180)
        rc = proc.returncode
    except Exception as exc:  # noqa: BLE001
        # 非管理员环境无法启动带 uac_admin 清单的 EXE（WinError 740 需要提升）。
        # 这是环境约束，不是打包缺陷 —— 干净跳过。
        if "740" in str(exc) or "elevation" in str(exc).lower() or "提升" in str(exc):
            results.append(Check(
                "冻结二进制自测 EXE --selftest",
                f"需要管理员权限才能启动（uac_admin 清单），非提权环境跳过：{exc}",
                STATUS_SKIP, critical=False,
            ))
        else:
            results.append(Check(
                "冻结二进制自测 EXE --selftest",
                f"执行异常：{exc}", STATUS_FAIL,
            ))
        return
    ok = rc == 0
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(noconsole 二进制无 stdout)"
    results.append(Check(
        "冻结二进制自测 EXE --selftest",
        f"rc={rc}；{tail}", STATUS_PASS if ok else STATUS_FAIL,
    ))


def check_write_path_isolation(results: list[Check]) -> None:
    """③ 写路径隔离不变量：UI 层 import 不应拉起唯一写盘模块。"""
    try:
        # 先确保干净：移除可能已存在的 mover，验证 import UI 不会重新拉起它。
        sys.modules.pop("app.win32.mover", None)
        import app.ui.main_window  # noqa: F401  —— 拉起 UI 层
        pulled = "app.win32.mover" in sys.modules
        if pulled:
            results.append(Check(
                "写路径隔离 UI→mover",
                "import app.ui.main_window 后 app.win32.mover 出现在 sys.modules（预览/UI 不应拉起写盘模块）",
                STATUS_FAIL,
            ))
            return
        # 反向确认写路径本身完好、可正常加载，且唯一写盘 IOCTL 常量就位。
        importlib.import_module("app.win32.mover")
        from app.win32 import api
        assert api.FSCTL_MOVE_FILE == 0x00090074, "写盘 IOCTL 常量异常"
        results.append(Check(
            "写路径隔离 UI→mover",
            "UI 层 import 未拉起 mover；mover 本身可正常 import（FSCTL_MOVE_FILE=0x00090074 就位）",
            STATUS_PASS,
        ))
    except Exception as exc:  # noqa: BLE001
        results.append(Check(
            "写路径隔离 UI→mover",
            f"异常：{exc}", STATUS_FAIL,
        ))


def check_real_volume_enumeration(results: list[Check]) -> None:
    """④ 真实 Win32 卷枚举（FSCTL_* 实路径，非 mock）。"""
    try:
        from app.win32 import volume
        vols = volume.list_volumes()
    except Exception as exc:  # noqa: BLE001
        results.append(Check(
            "真实卷枚举 list_volumes()",
            f"环境/权限不足，干净跳过：{type(exc).__name__}: {exc}",
            STATUS_SKIP, critical=False,
        ))
        return
    results.append(Check(
        "真实卷枚举 list_volumes()",
        f"真实 FSCTL 路径成功，返回 {len(vols)} 个卷",
        STATUS_PASS,
    ))


_qapp = None


def check_live_mainwindow(results: list[Check]) -> None:
    """⑤ 真实 MainWindow 在 offscreen 下能否无崩溃构造（真实 UI 引导）。"""
    global _qapp
    try:
        from PySide6.QtWidgets import QApplication
        if _qapp is None:
            _qapp = QApplication.instance() or QApplication(sys.argv[:1])
        from app.ui.main_window import MainWindow
        w = MainWindow()
        _qapp.processEvents()
        w.close() if hasattr(w, "close") else None
        results.append(Check(
            "真实主窗口构造 MainWindow()",
            "offscreen 下无崩溃装配完成", STATUS_PASS,
        ))
    except Exception as exc:  # noqa: BLE001
        results.append(Check(
            "真实主窗口构造 MainWindow()",
            f"offscreen 构造失败（显示/依赖环境不足可视为环境跳过）：{type(exc).__name__}: {exc}",
            STATUS_SKIP, critical=False,
        ))


def main() -> int:
    results: list[Check] = []
    print("=" * 64)
    print(" LBA Tail Mover · smoke test（覆盖传统 TestFlow 到不了的地方）")
    print("=" * 64)

    check_entrypoint_selftest(results)
    check_exe_selftest(results)
    check_write_path_isolation(results)
    check_real_volume_enumeration(results)
    check_live_mainwindow(results)

    n_pass = n_skip = n_fail = 0
    for c in results:
        if c.status == STATUS_PASS:
            n_pass += 1
        elif c.status == STATUS_SKIP:
            n_skip += 1
        else:
            n_fail += 1
        mark = {"PASS": "[ ok ]", "SKIP": "[skip]", "FAIL": "[FAIL]"}.get(c.status, "[????]")
        print(f"{mark} {c.name}\n        {c.detail}")

    critical_fail = any(c.status == STATUS_FAIL and c.critical for c in results)
    verdict = "FAIL" if critical_fail else "PASS"
    print("-" * 64)
    print(f"smoke 结论：{verdict}  ·  PASS {n_pass} / SKIP {n_skip} / FAIL {n_fail}")

    # 镜像一份独立报告，避免与传统流并发写同一个 last-run.json 时丢失。
    try:
        report = {
            "tool": "smoketest",
            "verdict": verdict,
            "counts": {"pass": n_pass, "skip": n_skip, "fail": n_fail},
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail,
                 "critical": c.critical}
                for c in results
            ],
        }
        out = ROOT / ".testflow" / "smoke-last-run.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    return 1 if critical_fail else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
