"""一键封包：图标 → 版本资源 → PyInstaller → wheel → zip → 校验和。

为什么要有这个脚本而不是让人记三条命令：封包的每一步都有**顺序依赖**
（图标要在 spec 之前生成、version 资源要在打包之前生成），而且产物必须
被**真的跑一遍**才算封完。源码树里 pytest 全绿不代表 exe 能起来——缺 Qt
插件、缺 hook、缺字体，都只在运行时才暴露。

用法::

    python packaging/build.py              # 全量
    python packaging/build.py --no-wheel   # 只出 exe
    python packaging/build.py --skip-verify# 跳过产物验证（不推荐）

退出码 0 表示产物已验证可用。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
APP_DIR = os.path.join(DIST, "LBAMover")
EXE_GUI = os.path.join(APP_DIR, "LBAMover.exe")
EXE_CLI = os.path.join(APP_DIR, "LBAMover-cli.exe")
sys.path.insert(0, os.path.join(ROOT, "src"))
from lbamover import VERSION_DISPLAY  # noqa: E402


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], *, env: dict | None = None, label: str = "") -> subprocess.CompletedProcess:
    # PYTHONUTF8：让冻结产物按 UTF-8 写 stdout，跨机器行为一致。
    # errors="replace"：即使它没照做（例如第三方 exe），也只是显示成替换符，
    # 绝不能让解码错误把真正的失败信息吞掉。
    full_env = {**os.environ, "PYTHONUTF8": "1", **(env or {})}
    log("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        raise SystemExit(f"[build] 失败（{label or cmd[0]}，退出码 {proc.returncode}）\n{tail}")
    return proc


def step_icon() -> None:
    log("生成图标")
    run([sys.executable, "packaging/make_icon.py"], label="icon")


def step_version_file() -> None:
    log("生成版本资源")
    run([sys.executable, "packaging/make_version_file.py"], label="version")


def step_pyinstaller() -> None:
    log("PyInstaller 打包（onedir，两个 exe 共用运行库）")
    t0 = time.monotonic()
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            DIST,
            "--workpath",
            BUILD,
            "lbamover.spec",
        ],
        label="pyinstaller",
    )
    log(f"打包完成，用时 {time.monotonic() - t0:.1f}s")
    for path in (EXE_GUI, EXE_CLI):
        if not os.path.isfile(path):
            raise SystemExit(f"[build] 缺少产物：{path}")


def step_verify() -> None:
    """产物验证：版本号、无头自检、GUI 冒烟。三项都必须过。"""
    log("验证：--version")
    proc = run([EXE_CLI, "--version"], label="exe --version")
    got = (proc.stdout or "").strip()
    if VERSION_DISPLAY not in got:
        raise SystemExit(f"[build] 版本不匹配：期望 {VERSION_DISPLAY}，实际 {got!r}")

    log("验证：引擎自检（合成卷 + 逐块校验）")
    run(
        [EXE_CLI, "selftest", "--blocks", "1024", "--files", "12"],
        env={"PYTHONUTF8": "1"},
        label="exe selftest",
    )

    for exe, how in ((EXE_CLI, "命令行版"), (EXE_GUI, "窗口版")):
        log(f"验证：{how} GUI 冒烟（无头建窗口 + 跑一次完整搬迁）")
        run(
            [exe, "smoke"] if exe == EXE_CLI else [exe, "--smoke"],
            env={"QT_QPA_PLATFORM": "offscreen"},
            label=f"exe smoke ({os.path.basename(exe)})",
        )
    log("产物验证通过")


def step_wheel() -> None:
    log("构建 wheel")
    run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", DIST],
        label="wheel",
    )


def step_zip() -> str:
    log("打包 zip")
    archive = os.path.join(DIST, f"LBAMover-{VERSION_DISPLAY}-win64")
    path = shutil.make_archive(archive, "zip", root_dir=DIST, base_dir="LBAMover")
    size = os.path.getsize(path) / 1048576
    log(f"zip -> {os.path.basename(path)} ({size:.1f} MiB)")
    return path


def step_checksums(paths: list[str]) -> str:
    out = os.path.join(DIST, "SHA256SUMS.txt")
    lines = []
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {os.path.basename(p)}")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log("校验和 -> SHA256SUMS.txt")
    return out


def dir_size_mb(path: str) -> float:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total / 1048576


def main(argv: list[str] | None = None) -> int:
    global DIST, APP_DIR, EXE_GUI, EXE_CLI

    ap = argparse.ArgumentParser(description="LBAMover 封包")
    ap.add_argument("--no-wheel", action="store_true", help="不构建 wheel")
    ap.add_argument("--skip-verify", action="store_true", help="跳过产物验证（不推荐）")
    ap.add_argument("--keep-build", action="store_true", help="保留 build/ 中间目录")
    ap.add_argument(
        "--dist",
        default=DIST,
        help="产物输出目录（默认 dist/）。程序正开着时 dist/LBAMover/*.exe 被占用"
        "清不掉，用这个参数换一个目录出包，互不影响",
    )
    args = ap.parse_args(argv)

    DIST = os.path.abspath(args.dist)
    APP_DIR = os.path.join(DIST, "LBAMover")
    EXE_GUI = os.path.join(APP_DIR, "LBAMover.exe")
    EXE_CLI = os.path.join(APP_DIR, "LBAMover-cli.exe")
    os.makedirs(DIST, exist_ok=True)

    log(f"目标版本 {VERSION_DISPLAY}")
    step_icon()
    step_version_file()
    step_pyinstaller()
    if not args.skip_verify:
        step_verify()
    else:
        log("已跳过产物验证")

    artifacts = [os.path.join(APP_DIR, "LBAMover.exe"), os.path.join(APP_DIR, "LBAMover-cli.exe")]
    if not args.no_wheel:
        step_wheel()
        for name in os.listdir(DIST):
            if name.endswith(".whl"):
                artifacts.append(os.path.join(DIST, name))

    archive = step_zip()
    artifacts.append(archive)
    step_checksums(artifacts)

    if not args.keep_build:
        shutil.rmtree(BUILD, ignore_errors=True)

    log("")
    log(f"完成：{VERSION_DISPLAY}")
    log(f"  界面      dist/LBAMover/LBAMover.exe")
    log(f"  命令行    dist/LBAMover/LBAMover-cli.exe")
    log(f"  目录体积  {dir_size_mb(APP_DIR):.1f} MiB")
    log(f"  分发包    dist/{os.path.basename(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
