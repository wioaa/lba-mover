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


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
