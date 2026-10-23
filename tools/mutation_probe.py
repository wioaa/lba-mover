"""变异探针：验证「回归钉子」是否真的会在行为被破坏时变红（1.0.0E）。

**为什么需要它**（本轮真实事故）：

* 就地变异（直接改仓库里的源文件再跑测试）有三个坑：
  ① 与并发的 testflow / 人工编辑互相干扰（同事看到 `M app/...` 会以为代码被改坏，
     并行跑测试还会撞 `INVALID (source-changed)`）；
  ② 进程被 kill 或脚本抛异常早退时，源码留在**变异态**（`finally` 还原属补救，
     不是预防）；
  ③ 用硬链接做「副本」时，某些操作会通过链接把唯一的原文件也删掉。
* 于是本工具**默认在影子副本里工作**：把 ``app/`` / ``tests/`` 拷到 ``tempfile``
  目录，改副本、在副本里跑测试，**真仓库全程只读**。这把这个风险从「靠脚本自觉
  还原」变成结构性保证。

用法::

    python tools/mutation_probe.py --manifest tools/mutations/cancel_semantics.json
    python tools/mutation_probe.py --manifest ... --keep-shadow      # 保留影子目录
    python tools/mutation_probe.py --manifest ... --in-place        # 逃生口（见下）

``--in-place`` 是显式逃生口：直接改真仓库文件再还原，**仅**用于必须验证文件级
副作用的场景（例如「进程被杀是否留下半写文件」）。启用后本工具强制要求
「逐条变异结束后 `git status --porcelain -- app/ tests/` 为空」，否则以
``EXIT_DIRTY`` 退出。

退出码：``0`` 全部符合期望 · ``1`` 有变异不符合期望 · ``2`` 基础设施错误
（锚点未命中 / 基线不通过 / 真仓库被改动 / in-place 还原不干净）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: 全部符合期望
EXIT_OK = 0
#: 至少一条变异的行为与 `expect` 不符（= 钉子有盲区或断言过硬）
EXIT_MISMATCH = 1
#: 基础设施错误：锚点未命中、基线不通过、真仓库被改动、还原不干净……
EXIT_ERROR = 2

#: 影子目录前缀（出现在临时目录里，便于人工清理）
SHADOW_PREFIX = "mutation-probe-"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Mutation:
    """一条变异：把 ``file`` 里的 ``old`` 换成 ``new``，然后跑 ``test``。

    Attributes:
        name: 人类可读的变异名（报告里原样打印）。
        file: 相对项目根的源文件路径。
        old: 必须**恰好出现一次**的原文锚点（用来保证变异可复现）。
        new: 替换后的文本。
        test: 相对项目根的测试文件路径（以脚本方式运行，退出码 0 = 通过）。
        expect: ``"fail"``（测试应当变红）或 ``"pass"``（测试应当仍绿）。
    """

    name: str
    file: str
    old: str
    new: str
    test: str
    expect: str = "fail"


@dataclass(frozen=True)
class Manifest:
    """变异清单。

    Attributes:
        copies: 需要拷进影子副本的顶层目录（默认 ``["app", "tests"]``）。
        timeout: 单个测试文件的超时秒数。
        mutations: 变异列表。
    """

    copies: Sequence[str]
    timeout: int
    mutations: Sequence[Mutation]


@dataclass
class MutationResult:
    """单条变异的执行结果（供打印与断言）。"""

    mutation: Mutation
    passed: bool
    failures: List[str]
    detail: str = ""

    @property
    def actual(self) -> str:
        """实际表现：``pass`` / ``fail``。"""
        return "pass" if self.passed else "fail"

    @property
    def matches(self) -> bool:
        """实际表现是否符合 ``expect``。"""
        return self.actual == self.mutation.expect

    @property
    def first_failure(self) -> str:
        """首个失败的用例名（无失败时给 ``-``），用于定位是「哪枚钉子」咬住了变异。"""
        return self.failures[0] if self.failures else "-"


# --------------------------------------------------------------------------- #
# 清单 / 仓库工具
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> Manifest:
    """读取并校验 JSON 清单。

    Args:
        path: 清单文件路径。

    Returns:
        解析后的 :class:`Manifest`。

    Raises:
        ValueError: 清单结构非法（缺字段 / ``expect`` 取值非法 / 变异为空）。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    copies = tuple(raw.get("copies") or ("app", "tests"))
    timeout = int(raw.get("timeout") or 600)
    mutations: List[Mutation] = []
    for index, item in enumerate(raw.get("mutations") or []):
        for key in ("name", "file", "old", "new", "test"):
            if not item.get(key):
                raise ValueError(f"清单第 {index + 1} 条缺少字段 {key!r}")
        expect = str(item.get("expect") or "fail").strip().lower()
        if expect not in ("fail", "pass"):
            raise ValueError(
                f"清单第 {index + 1} 条 expect={expect!r} 非法，只支持 'fail' / 'pass'"
            )
        mutations.append(
            Mutation(
                name=str(item["name"]),
                file=str(item["file"]),
                old=str(item["old"]),
                new=str(item["new"]),
                test=str(item["test"]),
                expect=expect,
            )
        )
    if not mutations:
        raise ValueError("清单里没有任何变异")
    return Manifest(copies=copies, timeout=timeout, mutations=tuple(mutations))


def project_root_from_tool() -> Path:
    """默认项目根 = 本文件所在目录的父目录（``tools/`` 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def git_hash_object(root: Path, relative: str) -> str:
    """取 ``git hash-object <relative>``（工作树版本），失败返回空串。

    用 blob 号而不是 mtime/size：它能和 ``git rev-parse HEAD:<path>`` 直接比对，
    也天生对「内容相同但行尾被转换」这类差异免疫（一旦被转换 blob 就变了）。
    """
    proc = subprocess.run(
        ["git", "hash-object", relative],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_status_porcelain(root: Path, paths: Sequence[str]) -> str:
    """取指定路径的 ``git status --porcelain`` 输出（空串 = 干净）。"""
    if not paths:
        return ""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout or "").strip()


def snapshot_blobs(root: Path, files: Sequence[str]) -> Dict[str, str]:
    """给一组相对路径拍一份 ``git hash-object`` 快照。"""
    return {relative: git_hash_object(root, relative) for relative in files}


# --------------------------------------------------------------------------- #
# 影子副本
# --------------------------------------------------------------------------- #
def make_shadow(root: Path, copies: Sequence[str]) -> Path:
    """把 ``copies`` 列出的目录拷进一个临时目录，返回该临时目录。

    Args:
        root: 项目根。
        copies: 需要拷贝的顶层目录名。

    Returns:
        影子目录路径（调用方负责 ``shutil.rmtree``，或交给 ``--keep-shadow`` 保留）。
    """
    shadow = Path(tempfile.mkdtemp(prefix=SHADOW_PREFIX))
    for name in copies:
        source = root / name
        if not source.is_dir():
            raise FileNotFoundError(f"清单要求的目录不存在：{source}")
        shutil.copytree(source, shadow / name, dirs_exist_ok=True)
    return shadow


# --------------------------------------------------------------------------- #
# 变异 / 测试
# --------------------------------------------------------------------------- #
def apply_mutation(workdir: Path, mutation: Mutation) -> None:
    """把一条变异写进 ``workdir``（影子或真仓库）里的目标文件。

    Args:
        workdir: 变异落点（影子目录或项目根）。
        mutation: 变异定义。

    Raises:
        ValueError: 锚点 ``old`` 未出现或出现多次（变异不可复现，必须人工修清单）。
    """
    target = workdir / mutation.file
    # 统一按 \n 读入，锚点不必关心 CRLF / LF
    text = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
    occurrences = text.count(mutation.old)
    if occurrences != 1:
        raise ValueError(
            f"锚点在 {mutation.file} 里出现 {occurrences} 次（要求恰好 1 次）："
            f"{mutation.old!r}"
        )
    target.write_text(text.replace(mutation.old, mutation.new, 1), encoding="utf-8", newline="\n")


def run_test(workdir: Path, test: str, timeout: int) -> Tuple[bool, str]:
    """在 ``workdir`` 里以脚本方式跑一个测试文件。

    Args:
        workdir: 工作目录（影子目录或项目根）。
        test: 相对 ``workdir`` 的测试文件路径。
        timeout: 超时秒数。

    Returns:
        ``(是否通过, 合并后的输出)``。
    """
    env = dict(os.environ)
    # 影子隔离：让 `import app` 命中影子副本，绝不回落到真仓库
    env["PYTHONPATH"] = str(workdir)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        proc = subprocess.run(
            [sys.executable, test],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout}s）：{test}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def parse_failures(output: str) -> List[str]:
    """从测试输出里抓 ``[FAIL] <用例名>``（本项目 tests/_harness.py 的格式）。"""
    failures: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("[FAIL]"):
            name = line[len("[FAIL]"):].strip()
            if name:
                failures.append(name)
    return failures


# --------------------------------------------------------------------------- #
# 两种运行模式
# --------------------------------------------------------------------------- #
def run_shadow(
    root: Path,
    manifest: Manifest,
    keep_shadow: bool,
) -> List[MutationResult]:
    """在影子副本里逐条跑变异（**默认模式，真仓库只读**）。

    Args:
        root: 项目根。
        manifest: 变异清单。
        keep_shadow: 结束时是否保留影子目录（便于人工排查）。

    Returns:
        逐条变异的结果。

    Raises:
        RuntimeError: 基线（未变异的影子副本）跑不过 —— 此时任何「变红」都不足
            为凭，必须先修基线。
    """
    shadow = make_shadow(root, manifest.copies)
    results: List[MutationResult] = []
    baseline_checked: set = set()
    try:
        for mutation in manifest.mutations:
            _reset_shadow(root, manifest.copies, shadow)
            if mutation.test not in baseline_checked:
                # 同一测试文件只校验一次基线：基线本来就红时，任何「变红」都不足为凭
                passed, output = run_test(shadow, mutation.test, manifest.timeout)
                if not passed:
                    raise RuntimeError(
                        f"基线不通过，无法判定变异效果：{mutation.test}\n{_tail(output)}"
                    )
                baseline_checked.add(mutation.test)
                _reset_shadow(root, manifest.copies, shadow)
            apply_mutation(shadow, mutation)
            passed, output = run_test(shadow, mutation.test, manifest.timeout)
            results.append(
                MutationResult(
                    mutation=mutation,
                    passed=passed,
                    failures=parse_failures(output),
                    detail=_tail(output),
                )
            )
        return results
    finally:
        if keep_shadow:
            print(f"[probe] 影子目录已保留：{shadow}")
        else:
            shutil.rmtree(shadow, ignore_errors=True)


def _reset_shadow(root: Path, copies: Sequence[str], shadow: Path) -> Path:
    """把影子目录重置为「真仓库当前内容」的一份干净拷贝。

    每条变异都从干净状态出发，避免上一条变异的残留影响判定。
    """
    for name in copies:
        target = shadow / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root / name, target)
    return shadow


def run_in_place(root: Path, manifest: Manifest) -> List[MutationResult]:
    """直接在真仓库里改文件并还原（**逃生口**，需要文件级副作用时才用）。

    Args:
        root: 项目根。
        manifest: 变异清单。

    Returns:
        逐条变异的结果。

    Raises:
        RuntimeError: 还原后工作树仍不干净（说明有变异残留，必须人工介入）。
    """
    results: List[MutationResult] = []
    try:
        for mutation in manifest.mutations:
            target = root / mutation.file
            original = target.read_bytes()
            try:
                apply_mutation(root, mutation)
                passed, output = run_test(root, mutation.test, manifest.timeout)
            finally:
                target.write_bytes(original)
            results.append(
                MutationResult(
                    mutation=mutation,
                    passed=passed,
                    failures=parse_failures(output),
                    detail=_tail(output),
                )
            )
    finally:
        dirty = git_status_porcelain(root, list(manifest.copies))
        if dirty:
            raise RuntimeError(
                "in-place 还原后工作树不干净，请人工检查：\n" + dirty
            )
    return results


def _tail(text: str, limit: int = 1200) -> str:
    """取输出尾部（只用于诊断打印，避免刷屏）。"""
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    """估算终端显示宽度（CJK 全角字符按 2 列算，用于表格对齐）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按**显示宽度**左对齐补空格（``str.ljust`` 对 CJK 会错位）。"""
    return text + " " * max(0, width - _display_width(text))


def print_table(results: Sequence[MutationResult]) -> None:
    """打印对齐报告表（可直接贴进评审 / 报告）。"""
    name_width = max([_display_width(result.mutation.name) for result in results] + [8])
    line_width = name_width + 44
    print()
    print(
        f"{_pad('变异', name_width)}  {'期望':<5} {'实际':<5} {'结论':<6} "
        "首个失败的钉子"
    )
    print("-" * line_width)
    for result in results:
        verdict = "符合" if result.matches else "不符合!!"
        print(
            f"{_pad(result.mutation.name, name_width)}  "
            f"{result.mutation.expect:<5} {result.actual:<5} {_pad(verdict, 6)} "
            f"{result.first_failure}"
        )
    bad = [result for result in results if not result.matches]
    print("-" * line_width)
    if bad:
        print(f"结论：{len(bad)} 条不符合期望 —— {[result.mutation.name for result in bad]}")
    else:
        print(f"结论：{len(results)} 条全部符合期望（期望 fail 的都红了、期望 pass 的都绿）")



# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="mutation_probe",
        description=(
            "变异探针：验证回归钉子是否真的会咬住「行为被破坏」的改动。"
            "默认在影子副本里跑，真仓库只读。"
        ),
    )
    parser.add_argument("--manifest", required=True, help="变异清单（JSON）")
    parser.add_argument(
        "--project-root",
        default=None,
        help="项目根目录（默认 = 本工具所在目录的上一级）",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="逃生口：直接改真仓库并还原（牺牲并发安全，仅用于验证文件级副作用）",
    )
    parser.add_argument(
        "--keep-shadow",
        action="store_true",
        help="保留影子副本目录便于排查（默认跑完即删）",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="把结果写成 JSON（**不要指向仓库内**，否则会污染工作树）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表（``None`` = 用 ``sys.argv[1:]``）。

    Returns:
        进程退出码：``0`` 全部符合期望 · ``1`` 有不符合 · ``2`` 基础设施错误。
    """
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve() if args.project_root else project_root_from_tool()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (root / manifest_path).resolve()

    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[probe] 清单读取失败：{exc}", file=sys.stderr)
        return EXIT_ERROR

    files = sorted({mutation.file for mutation in manifest.mutations})
    blobs_before = snapshot_blobs(root, files)
    missing = [relative for relative, blob in blobs_before.items() if not blob]
    if missing:
        print(f"[probe] 取不到 git blob（不在仓库里？）：{missing}", file=sys.stderr)
        return EXIT_ERROR

    print(f"[probe] 项目根：{root}")
    print(f"[probe] 清单：{manifest_path}（{len(manifest.mutations)} 条变异）")
    print(f"[probe] 模式：{'in-place（真仓库）' if args.in_place else 'shadow（真仓库只读）'}")

    try:
        results = (
            run_in_place(root, manifest) if args.in_place else run_shadow(root, manifest, args.keep_shadow)
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"[probe] 基础设施错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    blobs_after = snapshot_blobs(root, files)
    print_table(results)

    if blobs_after != blobs_before:
        changed = {
            relative: (blobs_before[relative], blobs_after[relative])
            for relative in blobs_before
            if blobs_before[relative] != blobs_after[relative]
        }
        print(f"[probe] 真仓库文件被改动，拒绝给出结论：{changed}", file=sys.stderr)
        return EXIT_ERROR
    print(f"[probe] 真仓库未被改动（git blob 前后一致，{len(files)} 个文件）")

    if args.in_place:
        dirty = git_status_porcelain(root, list(manifest.copies))
        if dirty:
            print(f"[probe] in-place 还原不干净：\n{dirty}", file=sys.stderr)
            return EXIT_ERROR
        print("[probe] in-place 还原干净（git status 为空）")

    if args.json_out:
        payload = [
            {
                "name": result.mutation.name,
                "file": result.mutation.file,
                "test": result.mutation.test,
                "expect": result.mutation.expect,
                "actual": result.actual,
                "matches": result.matches,
                "failures": result.failures,
            }
            for result in results
        ]
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[probe] 结果已写入：{args.json_out}")

    return EXIT_OK if all(result.matches for result in results) else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
