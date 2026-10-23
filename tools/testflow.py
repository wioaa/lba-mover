#!/usr/bin/env python3
"""testflow —— 通用测试流水线（单文件、零第三方依赖、跨项目复用）。

## 它解决什么问题

跑测试本身不难，难的是**跑完之后的一致性判断**：这次算过还是不过？失败了是
代码错、测试过时、还是环境没装好？跑的时候有人改了源码，结论还作数吗？
这些判断如果每次靠人（或 AI）读一屏输出临时决定，既慢又不一致。

testflow 把这些判断固化成规则，只输出**三态结论**：

    PASS     所有步骤退出码 0，且运行期间源码未变动
    FAIL     有步骤失败（细分 reason：tests-failed / build-failed / timeout / command-error）
    INVALID  运行期间被监视文件有变动 —— 结论不可信，与成功失败无关

结论之外，每条失败还带一个 `kind`（code / test / env / timeout / unknown）和
一句 `next`（下一步该干什么），所以调用方不需要再解释原始输出。

## 通用性

不绑定框架、不绑定语言。全部由 `.testflow.toml` 声明：

- `steps`：任意命令序列（C 项目可以是 build + ctest，Python 项目就是一步 pytest）
- `parser`：pytest / unittest / ctest / generic（generic 只看退出码）
- `watch`：参与"源码是否被改动"快照的 glob
- `artifacts`：跑完要清理的测试产物（只删文件与空目录，越界路径直接拒绝）
- `timeout`：单步超时

## 用法

    python tools/testflow.py                  # 跑默认 profile
    python tools/testflow.py -k eta           # 额外参数透传给最后一个步骤
    python tools/testflow.py --profile c      # 换成 C 流程
    python tools/testflow.py --json           # 只输出 JSON（给程序/AI 读）
    python tools/testflow.py --quiet          # 只输出一行结论
    python tools/testflow.py --no-clean       # 不清理产物，便于事后排查

退出码：0=PASS，1=FAIL，2=INVALID，3=配置/工具自身错误。

报告默认写到 `.testflow/last-run.json`（并在 `history.jsonl` 追加一行摘要）。
报告里的路径一律相对仓库根，`{python}` 会还原成占位符 —— 不要把本机用户名
或绝对路径带进可分享的文件里。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

SCHEMA_VERSION = 1
TOOL_NAME = "testflow"
REPORT_DIR = ".testflow"
DEFAULT_REPORT = f"{REPORT_DIR}/last-run.json"
HISTORY_KEEP = 200

# 退出码就是结论，调用方不必再解析文本
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INVALID = 2
EXIT_TOOL_ERROR = 3

CONFIG_NAMES = (".testflow.toml", "testflow.toml", ".testflow.json")

# 解析器注册表。内置 pytest / unittest / ctest / generic，新语言用
# register_parser() 追加即可 —— 核心判定逻辑（三态、归类、报告）不用改一行。
#
#   from testflow import register_parser
#   register_parser("go", parse_go, kind_rules=[("code", re.compile(r"panic: "))])
#
PARSER_FUNCS: dict[str, Any] = {}
PARSER_KIND_RULES: dict[str, list[tuple[str, "re.Pattern[str]"]]] = {}


def register_parser(
    name: str,
    func: Any,
    kind_rules: Optional[Iterable[tuple[str, "re.Pattern[str]"]]] = None,
) -> Any:
    """登记一个输出解析器；重复登记同名会覆盖（便于项目内定制）。

    ``func(text, exit_code) -> (Counts, [(用例名, 失败消息), ...])``

    ``kind_rules`` 是该语言特有的归类规则，形如 ``[("code", re.compile(r"panic: "))]``，
    会在通用关键词之前匹配 —— 有些语言的失败措辞只有本语言才认得出来
    （Go 的 ``panic:``、Rust 的 ``panicked at``）。
    """
    PARSER_FUNCS[str(name)] = func
    if kind_rules:
        PARSER_KIND_RULES[str(name)] = list(kind_rules)
    return func


def parser_names() -> tuple[str, ...]:
    return tuple(PARSER_FUNCS)

# 每类失败对应的下一步动作。写在这里而不是让调用方自己猜
NEXT_HINT = {
    "code": "改实现：源码抛异常或编译不过，测试本身大概率没问题",
    "test": "先确认实现是否符合预期：断言不成立可能是实现错，也可能是测试期望过时",
    "env": "补依赖 / 检查工具链与权限后重跑，别急着改代码",
    "timeout": "排查卡死点，或调大该步骤 timeout",
    "unknown": "看报告里该步骤的原始输出尾部再判断",
}


# ---------------------------------------------------------------- 数据结构


@dataclass
class Failure:
    """一条失败。step 表示它来自哪一步（C 项目的 build 失败也会进这里）。"""

    step: str
    test: str
    kind: str
    message: str
    next: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "test": self.test,
            "kind": self.kind,
            "message": self.message,
            "next": self.next or NEXT_HINT.get(self.kind, ""),
        }


@dataclass
class Counts:
    """用例计数。total 由四项推导，不单独存 —— 两个来源迟早会对不上。"""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def add(self, other: "Counts") -> None:
        self.passed += other.passed
        self.failed += other.failed
        self.errors += other.errors
        self.skipped += other.skipped

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
        }


@dataclass
class StepResult:
    name: str
    command: list[str]                 # 已脱敏，可直接写进报告
    parser: str
    exit_code: int
    duration_s: float = 0.0
    timed_out: bool = False
    output: str = ""                   # 原始输出（只在内存里，报告只留尾部）
    counts: Counts = field(default_factory=Counts)
    failures: list[Failure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "parser": self.parser,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "counts": self.counts.to_dict(),
            "failures": [f.to_dict() for f in self.failures],
        }


# ---------------------------------------------------------------- 配置


class ConfigError(RuntimeError):
    """配置缺失/写错。这类错误不该被当成"测试失败"，退出码单独区分。"""


def _read_toml(path: Path) -> dict:
    try:
        import tomllib as toml_module  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as toml_module  # 3.10 及以下：pip install tomli
        except ModuleNotFoundError as exc:  # pragma: no cover - 只在老 Python 上走到
            raise ConfigError(
                "当前 Python 没有 tomllib（需要 3.11+）。两个办法："
                "① pip install tomli；② 把配置写成同名的 .testflow.json"
            ) from exc
    with path.open("rb") as fh:
        return toml_module.load(fh)


def load_config(root: Path, explicit: Optional[str] = None) -> dict:
    """读配置。显式给了就用给的，否则按 CONFIG_NAMES 顺序找第一个存在的。"""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise ConfigError(f"找不到配置文件: {explicit}")
        return _read_toml(path) if path.suffix == ".toml" else json.loads(
            path.read_text(encoding="utf-8")
        )

    for name in CONFIG_NAMES:
        path = root / name
        if not path.exists():
            continue
        if path.suffix == ".toml":
            return _read_toml(path)
        return json.loads(path.read_text(encoding="utf-8"))

    raise ConfigError(
        f"找不到配置（{', '.join(CONFIG_NAMES)}）。最小配置示例见 tools/TESTFLOW.md"
    )


def get_profile(cfg: dict, name: str) -> dict:
    """取 ``[profiles.<name>]``。steps 是唯一必填项。"""
    profiles = cfg.get("profiles") or {}
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "（无）"
        raise ConfigError(f"没有 profile「{name}」。可用: {available}")
    profile = dict(profiles[name] or {})
    steps = profile.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigError(f"profile「{name}」缺少 steps（命令序列）")
    for idx, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("command"):
            raise ConfigError(f"profile「{name}」第 {idx + 1} 个步骤缺少 command")
        parser = step.get("parser", "generic")
        if parser not in PARSER_FUNCS:
            raise ConfigError(
                f"profile「{name}」第 {idx + 1} 个步骤 parser 不认识: {parser}"
                f"（可用 {', '.join(parser_names())}；新增语言见 tools/TESTFLOW.md）"
            )
    return profile


def expand_command(tokens: Sequence[str], root: Path, env: dict) -> list[str]:
    """展开 ``{python}`` / ``{root}`` / ``{env:NAME}`` 占位符。"""
    out: list[str] = []
    for token in tokens:
        token = str(token).replace("{python}", sys.executable).replace("{root}", str(root))
        token = re.sub(
            r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: env.get(m.group(1), ""),
            token,
        )
        out.append(token)
    return out


def sanitize_command(tokens: Sequence[str], root: Path) -> list[str]:
    """把绝对路径还原成占位符 —— 报告可能被贴到 issue 里，别带本机路径。"""
    out: list[str] = []
    for token in tokens:
        token = str(token)
        token = token.replace(sys.executable, "{python}")
        token = token.replace(str(root), "{root}")
        token = re.sub(r"(?i)[A-Z]:\\Users\\[^\\]+", "{home}", token)
        out.append(token)
    return out


# ---------------------------------------------------------------- 快照


def snapshot(root: Path, patterns: Sequence[str]) -> dict[str, tuple[int, int]]:
    """文件名 -> (大小, mtime_ns)。用来发现"跑测试期间源码被改过"。"""
    state: dict[str, tuple[int, int]] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            state[str(path.relative_to(root)).replace("\\", "/")] = (
                st.st_size,
                st.st_mtime_ns,
            )
    return state


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    changed = [k for k in before if after.get(k) != before[k]]
    changed += [k for k in after if k not in before]
    return sorted(set(changed))


# ---------------------------------------------------------------- 解析器
#
# 每个解析器只做两件事：数出 passed/failed/errors/skipped，列出失败条目。
# 拿不到计数就老实返回 0，不猜 —— 计数为 0 且退出码非 0 会被判成 command-error，
# 比编一个假数字好排查。

_PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")
_PYTEST_ITEM = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_CTEST_SUMMARY = re.compile(r"(\d+)%\s+tests passed,\s+(\d+)\s+tests failed out of\s+(\d+)")
_CTEST_ITEM = re.compile(r"^\s*\d+\s*-\s*(\S+)\s+\(([^)]*)\)")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
_UNITTEST_FAILED = re.compile(r"^FAILED \((.+)\)", re.M)
_UNITTEST_SKIPPED = re.compile(r"skipped=(\d+)")
_UNITTEST_ITEM = re.compile(r"^(FAIL|ERROR): (\S+)", re.M)


def parse_pytest(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    # summary 是最后一条带计数的行；从后往前找，避免被中间的进度行带偏
    for line in reversed(text.splitlines()):
        found = _PYTEST_COUNT.findall(line)
        if not found:
            continue
        for number, key in found:
            key = "errors" if key in ("error", "errors") else key
            if key in ("passed", "failed", "errors", "skipped"):
                setattr(counts, key, int(number))
        break

    items: list[tuple[str, str]] = []
    for raw in text.splitlines():
        match = _PYTEST_ITEM.match(raw.strip())
        if match:
            status, nodeid, message = match.group(1), match.group(2), (match.group(3) or "")
            items.append((nodeid, f"{status}: {message}".strip(": ").strip()))

    if counts.failed == 0 and counts.errors == 0:
        counts.failed = len([i for i in items if i[1].startswith("FAILED")])
        counts.errors = len([i for i in items if i[1].startswith("ERROR")])
    return counts, items


def parse_ctest(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()

    items: list[tuple[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        if "The following tests FAILED" in raw:
            in_block = True
            continue
        if in_block:
            item = _CTEST_ITEM.match(raw)
            if item:
                name, status = item.group(1), item.group(2)
                items.append((name, f"ctest: {status}"))
            elif raw.strip() and not raw.startswith((" ", "\t")):
                in_block = False

    match = None
    for match in _CTEST_SUMMARY.finditer(text):  # 取最后一条（可能有重复摘要）
        pass
    if match:
        _percent, failed, out_of = (int(g) for g in match.groups())
        not_run = sum(1 for _, message in items if "Not Run" in message)
        counts.failed = failed
        counts.skipped = not_run
        counts.passed = max(0, out_of - failed - not_run)
    return counts, items


def parse_unittest(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    ran = _UNITTEST_RAN.search(text)
    total = int(ran.group(1)) if ran else 0

    failed_line = _UNITTEST_FAILED.search(text)
    if failed_line:
        for pair in failed_line.group(1).split(","):
            key, _, value = pair.strip().partition("=")
            # unittest 写的是 failures=，字段叫 failed —— 别让它变成动态属性
            key = "failed" if key.strip() == "failures" else key.strip()
            if key in ("failed", "errors", "skipped"):
                setattr(counts, key, int(value or 0))
    skipped = _UNITTEST_SKIPPED.search(text)
    if skipped and not counts.skipped:
        counts.skipped = int(skipped.group(1))

    # unittest 只报总数，没解析到的部分按通过算（先解析 failed/errors 再倒推）
    if total:
        counts.passed = max(0, total - counts.failed - counts.errors - counts.skipped)

    items = [(m.group(2), m.group(1)) for m in _UNITTEST_ITEM.finditer(text)]
    return counts, items


def parse_generic(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    """不认识框架：不给计数、不编失败列表，只靠退出码。

    刻意不在这里造失败条目 —— 它没有任何信息可用，硬造一条 ``<command>``
    会让 run_step 里"按退出码判定 kind"的兜底逻辑失效（拿到的永远是 unknown）。
    """
    return Counts(), []


# 内置解析器注册。generic 必须始终存在 —— run_step 在遇到未注册名字时拿它兜底
for _name, _func in (
    ("pytest", parse_pytest),
    ("unittest", parse_unittest),
    ("ctest", parse_ctest),
    ("generic", parse_generic),
):
    register_parser(_name, _func)

# pytest 的非 0 退出码里有些不是"用例失败"，直接翻译成一句话，省得再查文档
_PYTEST_EXIT_NOTE = {
    2: "被中断",
    3: "pytest 内部错误",
    4: "命令行用法错误（参数不认识）",
    5: "没有收集到任何测试：-k 过滤掉了全部，或测试文件没被发现",
}


# ---------------------------------------------------------------- 失败归类

# timeout 只认明确的措辞：裸 "timeout" 会命中测试名（test_timeout_xxx），
# 把断言失败误判成超时。真正的超时由 run_step 的 timed_out 标志兜住。
_RE_TIMEOUT = re.compile(r"\btimed out\b|TimeoutExpired|超过 \d+ ?s? 未结束", re.I)
_RE_ENV_IMPORT = re.compile(
    r"ModuleNotFoundError|ImportError|No module named|Cannot find module|"
    r"command not found|is not recognized as an internal|"
    r"找不到命令|无法执行命令|不是内部或外部命令"
)
_RE_ENV_FS = re.compile(
    r"PermissionError|Access is denied|WinError\s+\d+|No space left|"
    r"\[Errno 13\]|\[Errno 2\]|FileNotFoundError"
)
_RE_CC_ERROR = re.compile(r"^\S+\.(?:c|h|cc|cpp|hpp):\d+:\d+:\s+(?:fatal )?error:", re.M)
_RE_ASSERT = re.compile(r"AssertionError|\bassert\b")
_RE_CODE_EXC = re.compile(
    r"\b(TypeError|AttributeError|KeyError|IndexError|NameError|"
    r"ZeroDivisionError|RuntimeError|ValueError|OSError|UnboundLocalError|"
    r"Segmentation fault|core dumped|undefined reference)\b"
)


def classify(text: str, parser: str = "generic") -> str:
    """把一段失败输出归类。顺序有意义：先环境，再超时，再断言，最后通用异常。

    - env 放最前：缺依赖时 traceback 里也会出现 ImportError 之外的杂音，先判定
    - AssertionError 归 test 而不是 code：它只说"期望与实际不符"，谁错要人看。
      这条会在 next 里写清楚，别让调用方误以为一定是测试的锅
    - 编译器报错只在 ctest/generic 下判 code，pytest 输出里出现 ``error:`` 不算
    """
    # 语言特有规则优先：panic:（Go）、panicked at（Rust）这类措辞只有本语言认得
    for kind, pattern in PARSER_KIND_RULES.get(parser, []):
        if pattern.search(text):
            return kind
    if _RE_TIMEOUT.search(text):
        return "timeout"
    if _RE_ENV_IMPORT.search(text):
        return "env"
    if _RE_ENV_FS.search(text):
        return "env"
    if parser in ("ctest", "generic") and _RE_CC_ERROR.search(text):
        return "code"
    if _RE_ASSERT.search(text):
        return "test"
    if _RE_CODE_EXC.search(text):
        return "code"
    return "unknown"


def tail(text: str, limit: int = 4000) -> str:
    """报告里只留尾部：失败详情通常在最末，前面几千行是进度。"""
    if len(text) <= limit:
        return text
    return "…（前略）\n" + text[-limit:]


# ---------------------------------------------------------------- 执行


def _decode(data: Optional[bytes]) -> str:
    """Windows 控制台可能是 GBK，pytest 一般是 UTF-8。逐级降级，别抛异常。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def run_step(
    step: dict, root: Path, timeout: float, extra: Sequence[str]
) -> StepResult:
    name = str(step.get("name") or step.get("command")[0])
    parser = step.get("parser", "generic")
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})

    command = expand_command(step["command"], root, env)
    if step.get("passthrough") and extra:
        command += list(extra)

    result = StepResult(
        name=name,
        command=sanitize_command(command, root),
        parser=parser,
        exit_code=0,
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout or None,
        )
        output = _decode(proc.stdout)
        result.exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        result.exit_code = -9
        output = _decode(exc.stdout) + _decode(exc.stderr)
        output += f"\n[testflow] 步骤「{name}」超过 {timeout:.0f}s 未结束，已中断"
    except FileNotFoundError:
        result.exit_code = 127
        output = f"[testflow] 找不到命令: {command[0]}"
    except OSError as exc:
        result.exit_code = 126
        output = f"[testflow] 无法执行命令: {exc}"
    result.duration_s = time.monotonic() - started
    result.output = output

    counts, items = (PARSER_FUNCS.get(parser) or parse_generic)(output, result.exit_code)
    result.counts = counts
    for test_id, message in items:
        blob = f"{test_id}\n{message}"
        kind = classify(blob, parser)
        result.failures.append(Failure(step=name, test=test_id, kind=kind, message=message[:500]))

    # 退出码非 0 但解析器一条失败都没给出（通用命令、配置错误、崩溃）：
    # 至少留一条，否则报告会出现"FAIL 但失败清单为空"这种没法排查的状态
    if result.exit_code != 0 and not result.failures:
        if result.timed_out:
            kind = "timeout"
            message = f"步骤「{name}」超过 {timeout:.0f}s 未结束"
        elif result.exit_code in (126, 127):
            kind = "env"
            message = f"命令无法执行（找不到可执行文件或没有权限）: {command[0]}"
        else:
            kind = classify(tail(output), parser)
            note = _PYTEST_EXIT_NOTE.get(result.exit_code, "") if parser == "pytest" else ""
            message = (
                f"退出码 {result.exit_code}：{note}"
                if note
                else f"退出码 {result.exit_code}，解析器 {parser} 未能定位具体用例"
            )
        result.failures.append(
            Failure(step=name, test="<command>", kind=kind, message=message)
        )
    return result


def clean_artifacts(root: Path, rels: Iterable[str]) -> list[str]:
    """只删配置里列出的文件/空目录。越界路径（.. 或绝对路径逃出 root）直接拒绝。"""
    removed: list[str] = []
    root_resolved = root.resolve()
    for rel in rels:
        path = (root / rel).resolve()
        if root_resolved != path and root_resolved not in path.parents:
            print(f"  ! 拒绝清理越界路径: {rel}")
            continue
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(rel))
            elif path.is_dir():
                path.rmdir()  # 非空会抛 OSError，交给下面的 except
                removed.append(f"{rel}/")
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------- 结论


def decide_verdict(
    results: Sequence[StepResult], changed: Sequence[str], planned: int
) -> tuple[str, str]:
    """三态判定。INVALID 优先于一切 —— 源码被改过时，成败都不作数。

    reason 细分：
      source-changed  运行期间被监视文件变动
      timeout         某步骤超时
      build-failed    前置步骤失败导致后续没跑（C 项目的 build 挂了）
      tests-failed    跑完了且有失败用例
      command-error   退出码非 0 但没有可识别的失败用例（用法错、环境崩）
      ok              全绿
    """
    if changed:
        return "INVALID", "source-changed"

    failed = [r for r in results if r.exit_code != 0]
    if not failed:
        return "PASS", "ok"
    if any(r.timed_out for r in failed):
        return "FAIL", "timeout"
    if len(results) < planned:
        return "FAIL", "build-failed"
    if any(r.counts.failed or r.counts.errors for r in failed):
        return "FAIL", "tests-failed"
    return "FAIL", "command-error"


def build_summary(
    verdict: str,
    reason: str,
    counts: Counts,
    results: Sequence[StepResult],
    duration: float,
    command: str,
) -> str:
    """一行话。调用方只读这行也能行动，不必再看别的字段。"""
    kinds: dict[str, int] = {}
    for result in results:
        for failure in result.failures:
            kinds[failure.kind] = kinds.get(failure.kind, 0) + 1

    parts = [f"[{verdict}] {reason}"]
    if counts.total:
        pieces = []
        for key, label in (("failed", "failed"), ("errors", "errors"), ("passed", "passed"), ("skipped", "skipped")):
            value = getattr(counts, key)
            if value:
                pieces.append(f"{value} {label}")
        if pieces:
            parts.append(" / ".join(pieces))
    if kinds:
        parts.append(" · ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))
    parts.append(f"{duration:.1f}s")
    parts.append(f"重跑: {command}")
    return " · ".join(parts)


# ---------------------------------------------------------------- 报告


def write_report(root: Path, report: dict, report_path: str) -> Path:
    path = Path(report_path)
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    history = path.parent / "history.jsonl"
    line = {
        "at": report["started_at"],
        "verdict": report["verdict"],
        "reason": report["reason"],
        "duration_s": report["duration_s"],
        "counts": report["counts"],
        "kinds": {},
    }
    for failure in report["failures"]:
        line["kinds"][failure["kind"]] = line["kinds"].get(failure["kind"], 0) + 1
    try:
        lines = history.read_text(encoding="utf-8").splitlines() if history.exists() else []
        lines.append(json.dumps(line, ensure_ascii=False))
        history.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- 输出


def print_human(report: dict, results: Sequence[StepResult], show_output: bool) -> None:
    bar = "=" * 64
    print(bar)
    print(f" testflow · {report['project']} · profile={report['profile']}")
    print(bar)
    for index, result in enumerate(results, 1):
        mark = "OK " if result.exit_code == 0 else "FAIL"
        print(
            f"[{index}/{report['planned_steps']}] {mark} {result.name}"
            f"  exit={result.exit_code}  {result.duration_s:.1f}s  ({result.parser})"
        )
        if result.counts.total:
            c = result.counts
            print(
                f"      用例 {c.total}：passed {c.passed} / failed {c.failed}"
                f" / errors {c.errors} / skipped {c.skipped}"
            )
        for failure in result.failures:
            print(f"      · [{failure.kind}] {failure.test}")
            if failure.message:
                print(f"        {failure.message[:200]}")
            print(f"        -> {failure.next or NEXT_HINT.get(failure.kind, '')}")
        if show_output and result.exit_code != 0:
            print("      ---- 输出尾部 ----")
            for line in tail(result.output, 1500).splitlines():
                print(f"      {line}")

    if report["source_changed"]:
        print("!" * 64)
        print("运行期间这些文件被改动，结论不可信：")
        for name in report["source_changed"][:10]:
            print(f"  - {name}")
        if len(report["source_changed"]) > 10:
            print(f"  … 还有 {len(report['source_changed']) - 10} 个")
        print("!" * 64)
    if report["artifacts_cleaned"]:
        print(f"已清理测试产物：{', '.join(report['artifacts_cleaned'])}")

    print("-" * 64)
    print(f"结论：{report['verdict']}  (reason={report['reason']})")
    print(report["summary"])
    print(f"报告：{report['report_path']}    退出码：{report['exit_code']}")


def _quote(token: str) -> str:
    """需要时加引号，别让重跑命令因为一个空格就跑偏。"""
    if not token or re.search(r"[\s\"'&|<>]", token):
        return f'"{token}"' if ('"' not in token) else f"'{token}'"
    return token


def command_for_rerun(argv: Sequence[str], profile: str, root: Path) -> str:
    """给一条能直接粘贴的重跑命令。

    脚本名取实际入口（从 run_tests.py 调用就给 run_tests.py，从 testflow.py
    调用就给 testflow.py），并相对仓库根显示 —— 换项目、换目录都还能用。
    """
    script = Path(sys.argv[0]).resolve()
    try:
        entry = str(script.relative_to(root)).replace("\\", "/")
    except ValueError:
        entry = script.name

    parts = ["python", entry]
    if profile != "default":
        parts += ["--profile", profile]
    extras = [a for a in argv if a not in ("--json", "--quiet", "--no-clean", "--show-output")]
    return " ".join(_quote(p) for p in parts + extras).strip()


# ---------------------------------------------------------------- 主流程


def parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="testflow", description="通用测试流水线：跑测试 + 三态结论 + 失败归类"
    )
    parser.add_argument("--config", help="配置文件路径（默认自动找 .testflow.toml/.json）")
    parser.add_argument("--profile", default="default", help="用哪个 profile（默认 default）")
    parser.add_argument("--timeout", type=float, default=None, help="单步超时秒数，覆盖配置")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论")
    parser.add_argument("--no-clean", action="store_true", help="不清理测试产物")
    parser.add_argument("--show-output", action="store_true", help="失败时打印输出尾部")
    args, extra = parser.parse_known_args(list(argv))
    return args, extra


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args, extra = parse_args(argv)
    root = Path.cwd()

    try:
        cfg = load_config(root, args.config)
        profile = get_profile(cfg, args.profile)
    except ConfigError as exc:
        print(f"[testflow] 配置错误：{exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    project = str((cfg.get("project") or {}).get("name") or root.name)
    report_path = str((cfg.get("project") or {}).get("report") or DEFAULT_REPORT)
    steps: list[dict] = profile["steps"]
    watch: list[str] = list(profile.get("watch") or [])
    artifacts: list[str] = list(profile.get("artifacts") or [])
    timeout = args.timeout if args.timeout is not None else float(profile.get("timeout", 900))

    # 额外参数透传给最后一个步骤（通常是测试本身，而不是 build）
    if extra and steps:
        steps[-1] = {**steps[-1], "passthrough": True}

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    before = snapshot(root, watch)
    started = time.monotonic()

    results: list[StepResult] = []
    for step in steps:
        result = run_step(step, root, timeout, extra)
        results.append(result)
        # 前置步骤失败就不再往后跑：build 挂了还去 ctest 只会多一屏噪音
        if result.exit_code != 0 and step.get("fail_fast", True):
            break
    duration = time.monotonic() - started

    changed = diff_snapshots(before, snapshot(root, watch))
    verdict, reason = decide_verdict(results, changed, len(steps))

    total = Counts()
    for result in results:
        total.add(result.counts)
    failures = [f.to_dict() for result in results for f in result.failures]

    exit_code = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(verdict, EXIT_INVALID)
    report = {
        "schema": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "project": project,
        "profile": args.profile,
        "verdict": verdict,
        "reason": reason,
        "exit_code": exit_code,
        "started_at": started_at,
        "duration_s": round(duration, 2),
        "planned_steps": len(steps),
        "steps": [r.to_dict() for r in results],
        "counts": total.to_dict(),
        "failures": failures,
        "source_changed": changed,
        "artifacts_cleaned": [],
        "watch_patterns": watch,
        "summary": build_summary(
            verdict, reason, total, results, duration,
            command_for_rerun(argv, args.profile, root),
        ),
        "report_path": report_path,
    }

    if not args.no_clean and artifacts:
        report["artifacts_cleaned"] = clean_artifacts(root, artifacts)

    path = write_report(root, report, report_path)
    report["report_path"] = str(path.relative_to(root)).replace("\\", "/") if path.is_relative_to(root) else str(path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.quiet:
        print(report["summary"])
    else:
        print_human(report, results, args.show_output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
