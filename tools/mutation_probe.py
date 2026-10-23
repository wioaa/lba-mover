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
import hashlib
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

    用 blob 号而不是 mtime/size：它能和 ``git rev-parse HEAD:<path>`` 直接比对。

    .. warning::

        **blob 看不见行尾差异**：本仓库 ``core.autocrlf=true`` 且无
        ``.gitattributes``，``git hash-object`` 会施加 clean filter（CRLF → LF），
        所以「内容被写成 LF / 原本是 CRLF」这种**字节已变**的残留，blob 与
        ``HEAD`` 完全一致、``git status`` 也干净。要证明「字节没被动过」，必须
        另配 :func:`sha256_of`（字节级摘要）兜底 —— 这两者互补，不能互相替代。
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


def sha256_of(path: Path) -> str:
    """取文件**字节级**摘要（对行尾、BOM、编码差异都敏感）。

    与 :func:`git_hash_object` 互补：后者被 clean filter 归一化，看不见行尾变化；
    前者是「这个字节流有没有被动过」的最终真相，用于变异后还原的自证。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


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


def git_head_bytes(root: Path, relative: str) -> bytes:
    """取 ``HEAD:<relative>`` 的**原始字节**（失败返回 ``b""``）。

    .. warning::

        **不要用它当「干净基线」**：``git show`` 拿到的是**仓库里存的字节**，而本仓库
        ``core.autocrlf=true``，blob 里的换行一律是 LF，工作树却是 CRLF。实测
        ``app/win32/mft_reader.py``：HEAD 字节 ``CRLF=0 / LF=666 / 28535 B``，工作树
        ``CRLF=666 / 28535+666 B`` —— 两者 sha256 **永远不同**。所以"工作树 vs HEAD
        字节"只能用来判断「与提交版是否有差异」，不能判断「是否被变异污染」。
        崩溃残留改用 :func:`check_crash_residue`（运行前落一份临时状态文件，下次运行
        比对）来抓。
    """
    proc = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=str(root),
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else b""


def sha256_bytes(data: bytes) -> str:
    """对字节流取 sha256（与 :func:`sha256_of` 口径一致）。"""
    return hashlib.sha256(data).hexdigest()


def state_file(root: Path) -> Path:
    """崩溃残留状态文件的位置（**临时目录，绝不写进仓库**）。

    用项目根路径的 hash 做键：同一个仓库的多次运行共用一个状态文件，不同仓库互不干扰。
    接受 ``str`` 或 ``Path``（测试与外部调用都方便）。
    """
    tag = sha256_bytes(str(Path(root).resolve()).encode("utf-8"))[:16]
    return Path(tempfile.gettempdir()) / f"mutation-probe-state-{tag}.json"


def load_state(root: Path) -> Optional[Dict[str, str]]:
    """读上次运行留下的字节摘要快照（没有则 ``None``）。"""
    path = state_file(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_state(root: Path, digests: Dict[str, str]) -> None:
    """落一份字节摘要快照（用于下次运行侦测「上次是否被强杀」）。"""
    state_file(root).write_text(
        json.dumps(digests, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clear_state(root: Path) -> None:
    """删除状态文件（一次成功运行结束后调用）。"""
    try:
        state_file(root).unlink()
    except OSError:
        pass


def check_crash_residue(root: Path, files: Sequence[str]) -> List[Dict[str, str]]:
    """侦测「上次运行被强杀（SIGKILL / Ctrl-C 后未收尾）」留下的字节残留。

    原理：运行前把目标文件的 **sha256** 落到临时状态文件；正常结束时删除它。若进程
    被强杀，``finally`` 不执行、状态文件留存 —— 下次运行一看「状态文件还在，且当前
    字节与记录不符」，即可判定上次留下了变异体。

    为什么必须用状态文件而不是「和 HEAD 比」：本仓库 ``core.autocrlf=true`` 时 blob
    存的是 LF、工作树是 CRLF，两者 sha256 永远不同（见 :func:`git_head_bytes`），
    拿它当干净基线会把**每一次干净运行**都误报成残留。

    Args:
        root: 项目根。
        files: 目标文件（相对路径）。

    Returns:
        残留清单，每项形如 ``{"file", "recorded", "current"}``（字节 sha256 前缀）。
    """
    recorded = load_state(root)
    if not recorded:
        return []
    current = snapshot_sha256(root, files)
    residues: List[Dict[str, str]] = []
    for relative in files:
        before = recorded.get(relative, "")
        now = current.get(relative, "")
        if before and now and before != now:
            residues.append(
                {"file": relative, "recorded": before[:16], "current": now[:16]}
            )
    return residues


def snapshot_blobs(root: Path, files: Sequence[str]) -> Dict[str, str]:
    """给一组相对路径拍一份 ``git hash-object`` 快照。"""
    return {relative: git_hash_object(root, relative) for relative in files}


def snapshot_sha256(root: Path, files: Sequence[str]) -> Dict[str, str]:
    """给一组相对路径拍一份 **字节级** sha256 快照（看得见行尾变化）。"""
    return {relative: sha256_of(root / relative) for relative in files}


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
def detect_newline(raw: bytes) -> bytes:
    """探测文件的行尾风格（``b"\\r\\n"`` 或 ``b"\\n"``）。"""
    return b"\r\n" if b"\r\n" in raw else b"\n"


def apply_mutation(workdir: Path, mutation: Mutation) -> None:
    """把一条变异写进 ``workdir``（影子或真仓库）里的目标文件。

    ★ **逐字节替换，不动行尾**（1.0.0E · QA-F1）：清单里的 ``old`` / ``new`` 一律
    用 ``\\n`` 书写，这里先按目标文件自身的行尾风格把它们转成字节，再在**原始字节**
    上做一次替换。好处是除被替换的那几行外，文件其余字节（含 CRLF、BOM、缩进）
    一个都不变 —— 于是「逻辑零改动的变异（``old == new``）写回后字节完全相同」，
    ``--in-place`` 的还原自证才有意义。

    历史教训：早先的实现是 ``read → 归一化成 \\n → write_text(newline="\\n")``，
    会把**整文件** CRLF 改成 LF；而本仓库 ``core.autocrlf=true`` + 无
    ``.gitattributes``，``git hash-object`` 的 clean filter 恰好把行尾差异吃掉 →
    blob 与 ``git status`` 双双"看不见"，`--in-place` 被 Ctrl-C 打断时的残留
    就成了哑弹。所以这里必须在字节层面工作，并用 sha256 兜底自证。

    Args:
        workdir: 变异落点（影子目录或项目根）。
        mutation: 变异定义（``old`` / ``new`` 用 ``\\n`` 书写）。

    Raises:
        ValueError: 锚点 ``old`` 在该文件里出现次数不为 1（变异不可复现，须人工修
            清单）；或该文件行尾风格与锚点不匹配（混合行尾文件请先在清单里改用
            不含换行的锚点）。
    """
    target = workdir / mutation.file
    raw = target.read_bytes()
    newline = detect_newline(raw)

    def _to_bytes(text: str) -> bytes:
        """把清单里的 ``\\n`` 文本转成目标文件行尾风格的字节。"""
        return text.replace("\n", newline.decode("ascii")).encode("utf-8")

    old_bytes = _to_bytes(mutation.old)
    new_bytes = _to_bytes(mutation.new)
    occurrences = raw.count(old_bytes)
    if occurrences != 1:
        raise ValueError(
            f"锚点在 {mutation.file} 里出现 {occurrences} 次（要求恰好 1 次）："
            f"{mutation.old!r}（该文件行尾={newline!r}）"
        )
    target.write_bytes(raw.replace(old_bytes, new_bytes, 1))


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

    还原自证是**双重**的：每条变异还原后立刻比对 ``sha256``（字节级，看得见行尾
    变化），全部跑完再核对 ``git status --porcelain -- <copies>``。只用后者会漏 ——
    clean filter 会吃掉行尾差异（见 :func:`git_hash_object` 的警告）。

    Args:
        root: 项目根。
        manifest: 变异清单。

    Returns:
        逐条变异的结果。

    Raises:
        RuntimeError: 还原后**字节**不一致（说明有变异残留，必须人工介入）。
        BaseException: 循环里原有的异常**原样向上抛**（不被还原校验掩盖）。
    """
    results: List[MutationResult] = []
    try:
        for mutation in manifest.mutations:
            target = root / mutation.file
            original = target.read_bytes()
            original_sha = hashlib.sha256(original).hexdigest()
            try:
                apply_mutation(root, mutation)
                passed, output = run_test(root, mutation.test, manifest.timeout)
            finally:
                target.write_bytes(original)
            restored_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            if restored_sha != original_sha:
                raise RuntimeError(
                    f"in-place 还原后**字节**不一致：{mutation.file}"
                    f"（{original_sha[:16]} → {restored_sha[:16]}）"
                )
            results.append(
                MutationResult(
                    mutation=mutation,
                    passed=passed,
                    failures=parse_failures(output),
                    detail=_tail(output),
                )
            )
    except BaseException:
        # 原异常优先：这里只做"附带体检"，打印后原样抛出，绝不掩盖真实错误
        dirty = git_status_porcelain(root, list(manifest.copies))
        if dirty:
            print(
                "[probe] 警告：in-place 异常退出后工作树不干净（原异常优先保留）：\n"
                + dirty,
                file=sys.stderr,
            )
        raise
    else:
        dirty = git_status_porcelain(root, list(manifest.copies))
        if dirty:
            raise RuntimeError("in-place 还原后工作树不干净，请人工检查：\n" + dirty)
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
    sha_before = snapshot_sha256(root, files)
    missing = [relative for relative, blob in blobs_before.items() if not blob]
    if missing:
        print(f"[probe] 取不到 git blob（不在仓库里？）：{missing}", file=sys.stderr)
        return EXIT_ERROR
    missing = [relative for relative, digest in sha_before.items() if not digest]
    if missing:
        print(f"[probe] 取不到文件字节摘要：{missing}", file=sys.stderr)
        return EXIT_ERROR

    print(f"[probe] 项目根：{root}")
    print(f"[probe] 清单：{manifest_path}（{len(manifest.mutations)} 条变异）")
    print(f"[probe] 模式：{'in-place（真仓库）' if args.in_place else 'shadow（真仓库只读）'}")

    # ★ 起跑前先查「上次被强杀留下的残留」：正常结束时状态文件会被删；它还在且字节
    # 与记录不符 → 上次的变异体还在盘上。此时**拒绝运行**，因为「跑前跑后一致」会把
    # 这份残留当成正常基线，自证彻底失效（这正是 F1 的坑：残留只改了行尾，
    # git status 与 git hash-object 都看不见）。
    residues = check_crash_residue(root, files)
    if residues:
        for item in residues:
            print(
                f"[probe] 发现崩溃残留（上次运行未收尾）：{item['file']} "
                f"记录={item['recorded']} 当前={item['current']}",
                file=sys.stderr,
            )
        print(
            "[probe] 拒绝运行：请先 `git checkout -- <file>` 恢复字节，"
            f"或确认无损后删除状态文件 {state_file(root)}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    save_state(root, sha_before)

    try:
        results = (
            run_in_place(root, manifest) if args.in_place else run_shadow(root, manifest, args.keep_shadow)
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"[probe] 基础设施错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    blobs_after = snapshot_blobs(root, files)
    sha_after = snapshot_sha256(root, files)
    print_table(results)

    if blobs_after != blobs_before or sha_after != sha_before:
        changed = {
            relative: {
                "blob": (blobs_before[relative], blobs_after[relative]),
                "sha256": (sha_before[relative][:16], sha_after[relative][:16]),
            }
            for relative in blobs_before
            if blobs_before[relative] != blobs_after[relative]
            or sha_before[relative] != sha_after[relative]
        }
        print(f"[probe] 真仓库文件被改动，拒绝给出结论：{changed}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"[probe] 真仓库未被改动（git blob + sha256 前后一致，{len(files)} 个文件）"
    )

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

    # 正常收尾（字节与进来时一致）→ 清掉状态文件，下次运行从干净基线开始。
    # 注意：上面的 EXIT_ERROR 分支都**不**清除，好让下次运行能认出"上次没跑完"。
    clear_state(root)
    return EXIT_OK if all(result.matches for result in results) else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
