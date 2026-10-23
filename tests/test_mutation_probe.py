"""`tools/mutation_probe.py` 的自测（1.0.0E · QA-F1 回归钉）。

**为什么需要它**：变异探针的唯一价值就是「它给出的结论可信」，而 F1 那个洞恰好让
它**自己的安全网变哑** —— 早先的 ``apply_mutation`` 把整文件 CRLF 归一化成 LF，
而本仓库 ``core.autocrlf=true`` + 无 ``.gitattributes``，``git hash-object`` 的 clean
filter 会把行尾差异吃掉，于是「字节已变」的残留同时满足 `git status` 干净 + blob
一致。本文件把「写回必须逐字节保真」变成钉子：

1. **逻辑零改动的变异**（``old == new``）写回后，字节完全不变（sha256 相同）——
   这是 F1 的验收点；
2. 真正变异时**只改被替换的那几行**，其余字节（含 CRLF）一个不动；
3. 锚点必须恰好出现一次（0 次 / 2 次都报错），保证变异可复现；
4. 清单校验：``expect`` 只接受 ``fail`` / ``pass``，缺字段要报错。

全部在临时目录里跑，不碰仓库任何文件。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import mutation_probe  # noqa: E402

#: 带 CRLF 的样例源码（模拟本仓库的工作树行尾）
CRLF_SOURCE = (
    "def scan():\r\n"
    "    for number in range(10):\r\n"
    "        if should_cancel():\r\n"
    "            mark_cancelled(outcome)\r\n"
    "            break\r\n"
    "    return 1\r\n"
)

ANCHOR = "            mark_cancelled(outcome)\n            break\n"


def _write_sample(directory: str, text: str) -> str:
    """在临时目录里写一份样例源文件，返回绝对路径。"""
    path = os.path.join(directory, "sample.py")
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return path


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _mutation(old: str, new: str) -> mutation_probe.Mutation:
    """构造一条指向 ``sample.py`` 的变异。"""
    return mutation_probe.Mutation(
        name="selftest",
        file="sample.py",
        old=old,
        new=new,
        test="unused.py",
        expect="fail",
    )


def test_apply_mutation_is_byte_identical_when_logic_unchanged() -> None:
    """F1 验收：``old == new`` 时写回字节必须完全相同（行尾不许被转换）。"""
    with tempfile.TemporaryDirectory() as directory:
        path = _write_sample(directory, CRLF_SOURCE)
        before = _sha256(path)
        mutation_probe.apply_mutation(mutation_probe.Path(directory), _mutation(ANCHOR, ANCHOR))
        after = _sha256(path)

        assert before == after, "逻辑零改动的变异写回后字节变了 —— 行尾被转换（F1 回归）"
        with open(path, "rb") as handle:
            raw = handle.read()
        assert raw.count(b"\r\n") == CRLF_SOURCE.count("\r\n"), "CRLF 行尾数必须原样保留"


def test_apply_mutation_preserves_untouched_bytes() -> None:
    """真变异时只改被替换的那几行，其余字节（含 CRLF）一个不动。"""
    replacement = "            mark_cancelled(outcome)\n            break\n            probe = 1\n"
    with tempfile.TemporaryDirectory() as directory:
        path = _write_sample(directory, CRLF_SOURCE)
        mutation_probe.apply_mutation(
            mutation_probe.Path(directory), _mutation(ANCHOR, replacement)
        )
        with open(path, "rb") as handle:
            raw = handle.read()

        expected_crlf = CRLF_SOURCE.count("\r\n") + 1        # 新增 1 行也应是 CRLF
        assert raw.count(b"\r\n") == expected_crlf, "新增行必须沿用原文件行尾"
        assert b"\n" not in raw.replace(b"\r\n", b""), "不得出现裸 LF（整文件行尾被转换）"
        assert b"probe = 1" in raw


def test_apply_mutation_requires_exactly_one_anchor() -> None:
    """锚点必须恰好出现一次（0 次 / 2 次都算清单写错，变异不可复现）。"""
    with tempfile.TemporaryDirectory() as directory:
        _write_sample(directory, CRLF_SOURCE)
        root = mutation_probe.Path(directory)
        for anchor in ("这段代码不存在\n", "return 1\n    return 1\n"):
            try:
                mutation_probe.apply_mutation(root, _mutation(anchor, "x\n"))
            except ValueError as exc:
                assert "恰好 1 次" in str(exc), str(exc)
            else:
                raise AssertionError(f"锚点 {anchor!r} 不唯一却没有报错")


def test_load_manifest_rejects_bad_expect(tmp_path=None) -> None:
    """清单校验：``expect`` 只接受 fail / pass。"""
    import json

    with tempfile.TemporaryDirectory() as directory:
        path = mutation_probe.Path(directory) / "m.json"
        path.write_text(
            json.dumps(
                {
                    "mutations": [
                        {
                            "name": "x",
                            "file": "a.py",
                            "old": "a",
                            "new": "b",
                            "test": "t.py",
                            "expect": "nope",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        try:
            mutation_probe.load_manifest(path)
        except ValueError as exc:
            assert "expect" in str(exc), str(exc)
        else:
            raise AssertionError("非法 expect 未被拒绝")


def test_crash_residue_is_detected_via_state_file() -> None:
    """崩溃残留模拟：施加变异**不还原**（模拟 SIGKILL），下次运行自证必须报不一致。

    这是 F1 的另一半：``--in-place`` 被 SIGKILL 时 ``finally`` 不执行，盘上留下变异体；
    若变异体只改了行尾，``git status`` 与 ``git hash-object`` 都说"干净"
    （``core.autocrlf=true`` 的 clean filter 吃掉差异）。所以探针在**运行前**把目标
    文件的字节 sha256 落到临时状态文件、正常结束才删；下次运行发现「状态文件还在 +
    字节不符」即判定残留。本用例把这个生命周期走一遍（不真杀进程）。
    """
    with tempfile.TemporaryDirectory() as directory:
        root = mutation_probe.Path(directory)
        target = root / "sample.py"
        with open(target, "wb") as handle:
            handle.write(CRLF_SOURCE.encode("utf-8"))
        files = ["sample.py"]

        mutation_probe.clear_state(root)
        assert mutation_probe.check_crash_residue(root, files) == [], "无状态文件时不该报残留"

        # 运行前落状态（模拟一次正常启动）
        mutation_probe.save_state(root, mutation_probe.snapshot_sha256(root, files))
        assert mutation_probe.check_crash_residue(root, files) == [], "字节没变不该报残留"

        # 模拟「变异体留在盘上」：整文件 CRLF → LF（逻辑不变，git 看不见）
        target.write_bytes(CRLF_SOURCE.replace("\r\n", "\n").encode("utf-8"))

        residues = mutation_probe.check_crash_residue(root, files)
        assert len(residues) == 1, f"字节级自证必须察觉残留，实际 {residues}"
        assert residues[0]["file"] == "sample.py", residues
        assert residues[0]["recorded"] != residues[0]["current"], residues

        # 正常收尾（还原 + 清状态）后应恢复干净
        target.write_bytes(CRLF_SOURCE.encode("utf-8"))
        mutation_probe.clear_state(root)
        assert mutation_probe.check_crash_residue(root, files) == [], "清理后不该再报残留"


def test_head_bytes_are_lf_but_worktree_is_crlf() -> None:
    """锁定一个**踩过的坑**：``git show HEAD:<path>`` 是 LF，工作树是 CRLF。

    仓库 ``core.autocrlf=true``、无 ``.gitattributes`` → blob 里换行一律 LF，工作树
    却是 CRLF（本仓库实测 ``mft_reader.py``：HEAD `CRLF=0/LF=666/28535B`，工作树
    `CRLF=666/29201B`）。因此「工作树字节 vs HEAD 字节」**永远不同**，不能当"干净"
    的判据 —— 谁再想用它抓崩溃残留，会得到"每次运行都报残留"的假阳性。
    本用例只是把这条事实钉住，防止后人重犯（见 `git_head_bytes` 的 warning）。
    """
    import subprocess

    with tempfile.TemporaryDirectory() as directory:
        root = mutation_probe.Path(directory)
        target = root / "sample.py"
        with open(target, "wb") as handle:
            handle.write(CRLF_SOURCE.encode("utf-8"))
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "core.autocrlf", "true"],
            ["git", "add", "-A"],
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        ):
            subprocess.run(cmd, cwd=directory, check=True, capture_output=True)

        head_bytes = mutation_probe.git_head_bytes(root, "sample.py")
        work_bytes = target.read_bytes()
        assert b"\r\n" not in head_bytes, "HEAD 里的字节应当是 LF（clean filter 存 LF）"
        assert b"\r\n" in work_bytes, "工作树应当是 CRLF（checkout 转换）"
        assert mutation_probe.sha256_bytes(head_bytes) != mutation_probe.sha256_bytes(
            work_bytes
        ), "两者字节必然不同 —— 正因如此，不能用 HEAD 字节当干净基线"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
