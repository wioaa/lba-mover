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
- `parser`：pytest / unittest / ctest / generic / tap / gotest / cargo / jest / junit
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

## 覆盖范围（v2 增补）

单一"跑一遍单元测试"覆盖不到常见测试方案里的其他层。v2 在这个文件里补齐了
六层，全部可选，不配就是 v1 的行为：

1. **框架层**：解析器从 4 个扩到 9 个 —— pytest / unittest / ctest / generic /
   tap / gotest / cargo / jest / junit。`junit` 读报告文件（JUnit XML），
   覆盖 Java、.NET、Node、C++ 等"结果落在文件里"的框架。
2. **阶段层**：步骤可以标 `required = false` 变成**审计项**（lint、类型检查、
   复杂度扫描），失败只记录不判定；`--strict` 才把它们算进结论。
3. **环境层**：`[preflight]` 环境自检（Python 版本、可执行文件、模块、路径、
   环境变量），不过关就不跑测试 —— 否则一屏 red 全是同一个环境问题。
4. **覆盖率层**：`[coverage]` 读标准覆盖率报告（coverage.py JSON、istanbul
   JSON、Cobertura XML），按总门槛与逐文件门槛判定。
5. **套件完整层**：`[inventory]` 用 `--collect-only` 枚举实际收集到的用例，
   与磁盘上的测试文件对账，报出"文件在但一个用例都没被收集"的漏测。
6. **历史层**：与上一次运行对比（新增失败 / 已修复 / 耗时漂移），
   `--repeat N` 重复跑识别 flaky，`--junit` 输出 CI 能读的 JUnit XML。

新增的 `reason`：`preflight-failed` / `coverage-below-min` / `orphan-tests` /
`audit-failed` / `regression`。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

SCHEMA_VERSION = 2
TOOL_NAME = "testflow"
REPORT_DIR = ".testflow"
DEFAULT_REPORT = f"{REPORT_DIR}/last-run.json"
HISTORY_KEEP = 200
DEFAULT_JUNIT = f"{REPORT_DIR}/junit.xml"

# 步骤默认是否"把关"：required = true 失败即判 FAIL；false 是审计项（lint /
# 类型检查 / 复杂度扫描），失败只记录。--strict 或 profile 里 strict = true 才升级。
STEP_REQUIRED_DEFAULT = True

# 预检不过时理由；与三态判定里的 reason 保持一致口径
REASON_PREFLIGHT = "preflight-failed"

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


# 文件型解析器：有的框架不往 stdout 打结果，而是写一份标准报告文件
# （JUnit XML、TRX、ctest XML）。这类解析器签名不同 —— 读文件，不是读文本：
#
#   [[profiles.default.steps]]
#   name = "junit"
#   command = ["{python}", "-m", "pytest", "--junitxml=build/junit.xml"]
#   parser = "junit"
#   report = "build/junit.xml"
#
# 报告文件缺失时按环境问题处理：有报告格式约定却没产出报告，通常是命令写错。
PARSER_FILE_FUNCS: dict[str, Any] = {}


def register_file_parser(name: str, func: Any) -> Any:
    """登记一个读报告文件的解析器：``func(path) -> (Counts, [(用例名, 消息)])``。"""
    PARSER_FILE_FUNCS[str(name)] = func
    return func


def file_parser_names() -> tuple[str, ...]:
    return tuple(PARSER_FILE_FUNCS)

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
    required: bool = True              # 审计项（lint / 类型检查）为 False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "parser": self.parser,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "required": self.required,
            "counts": self.counts.to_dict(),
            "failures": [f.to_dict() for f in self.failures],
        }


@dataclass
class CheckResult:
    """一条预检结果。环境不对就先拦下，别让后面的失败看起来像代码问题。"""

    name: str
    ok: bool
    kind: str = "env"
    detail: str = ""
    next: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "kind": self.kind,
            "detail": self.detail,
            "next": self.next or NEXT_HINT.get(self.kind, ""),
            "required": self.required,
        }


@dataclass
class CoverageResult:
    """从标准覆盖率报告里读到的数字。

    生成报告是项目自己那一步的事（coverage run / jest --coverage / coverlet），
    这里只负责按门槛判定 —— 否则工具就得内置一堆语言生态。
    """

    percent: float = 0.0
    covered: int = 0
    total: int = 0
    report: str = ""
    min_percent: float = 0.0
    below: list[str] = field(default_factory=list)   # 低于逐文件门槛的文件

    @property
    def ok(self) -> bool:
        return not self.below and self.percent >= self.min_percent

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": round(self.percent, 2),
            "covered_lines": self.covered,
            "total_lines": self.total,
            "min_percent": self.min_percent,
            "ok": self.ok,
            "below": list(self.below),
            "report": self.report,
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
        known = parser in PARSER_FUNCS or parser in PARSER_FILE_FUNCS
        if not known:
            all_names = ", ".join(parser_names() + file_parser_names())
            raise ConfigError(
                f"profile「{name}」第 {idx + 1} 个步骤 parser 不认识: {parser}"
                f"（可用 {all_names}；新增语言见 tools/TESTFLOW.md）"
            )
        if parser in PARSER_FILE_FUNCS and not (step.get("report") or step.get("junit")):
            raise ConfigError(
                f"profile「{name}」第 {idx + 1} 个步骤用了文件型解析器 {parser}，"
                "但没有给 report（报告文件路径）"
            )

    preflight = profile.get("preflight")
    if preflight is not None:
        checks = preflight.get("checks") if isinstance(preflight, dict) else preflight
        if not isinstance(checks, list) or not checks:
            raise ConfigError(
                f"profile「{name}」的 [preflight] 需要 checks 列表"
                "（可以写成 preflight = [...] 或 [preflight.checks] ...）"
            )
        for idx, check in enumerate(checks):
            if not isinstance(check, dict) or not check.get("type"):
                raise ConfigError(f"profile「{name}」preflight 第 {idx + 1} 项缺少 type")

    coverage = profile.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, dict):
            raise ConfigError(f"profile「{name}」的 [coverage] 必须是表")
        try:
            float(coverage.get("min", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"profile「{name}」coverage.min 不是数字") from exc

    inventory = profile.get("inventory")
    if inventory is not None and not isinstance(inventory, dict):
        raise ConfigError(f"profile「{name}」的 [inventory] 必须是表")
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


# ---- TAP（node --test、prove、bats、shell 的通用输出格式）----
_TAP_ITEM = re.compile(
    r"^(ok|not ok)\b\s*(\d+)?\s*(?:-\s*)?(.*?)(?:\s+#\s*(SKIP|TODO)\b.*)?$", re.I
)


def parse_tap(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    """TAP 12/13/14：``ok 1 - name`` / ``not ok 2 - name # TODO``。

    ``# TODO`` 按规范不算失败（已知问题），归到 skipped —— 否则老项目永远是红的。
    """
    counts = Counts()
    items: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _TAP_ITEM.match(line)
        if not match:
            continue
        mark, _num = match.group(1), match.group(2)
        name = (match.group(3) or "").strip() or "<unnamed>"
        directive = (match.group(4) or "").upper()
        if directive:
            counts.skipped += 1
        elif mark.lower() == "ok":
            counts.passed += 1
        else:
            counts.failed += 1
            items.append((name, "not ok"))
    return counts, items


# ---- Go：go test -v 的 --- PASS/FAIL/SKIP 行，或 -json 之外的包级结论 ----
_GO_ITEM = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.M)
_GO_PKG = re.compile(r"^(ok|FAIL)\s+(\S+)\s+[\d.]+s", re.M)


def parse_gotest(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    items: list[tuple[str, str]] = []
    for match in _GO_ITEM.finditer(text):
        status, name = match.group(1), match.group(2)
        if status == "PASS":
            counts.passed += 1
        elif status == "SKIP":
            counts.skipped += 1
        else:
            counts.failed += 1
            items.append((name, "go test: FAIL"))
    if counts.total == 0:
        # 没加 -v 时只剩包级结论：至少把失败的包报出来，别让调用方去翻输出
        for match in _GO_PKG.finditer(text):
            if match.group(1) == "FAIL":
                items.append((match.group(2), "go test: 包内有用例失败（加 -v 看具体用例）"))
        counts.failed = len(items)
    return counts, items


# ---- Rust：cargo test 的 test result 行 + failures: 区块 ----
_CARGO_RESULT = re.compile(
    r"test result: (\w+)\.\s+(\d+) passed;\s+(\d+) failed;\s+(\d+) ignored"
)


def parse_cargo(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    last = None
    for match in _CARGO_RESULT.finditer(text):
        last = match  # 多个测试目标会各打一行；取最后一行（汇总行在最后）
    if last:
        counts.passed = int(last.group(2))
        counts.failed = int(last.group(3))
        counts.skipped = int(last.group(4))

    items: list[tuple[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        if raw.strip() == "failures:":
            in_block = True
            continue
        if in_block:
            if not raw.strip():
                in_block = False
                continue
            items.append((raw.strip(), "cargo test: FAILED"))
    return counts, items


# ---- Jest / Vitest：``Tests: 1 failed, 2 passed, 3 total`` ----
_JEST_SUMMARY = re.compile(r"^Tests:\s+(.*)$", re.M)
_JEST_ITEM = re.compile(r"^\s*(?:✕|×|●)\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")


def parse_jest(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    items: list[tuple[str, str]] = []
    for raw in text.splitlines():
        match = _JEST_ITEM.match(raw)
        if match:
            items.append((match.group(1).strip(), "jest: FAILED"))
    summary = _JEST_SUMMARY.search(text)
    if summary:
        for part in summary.group(1).split(","):
            match = re.match(r"(\d+)\s+(failed|passed|skipped|todo|total)", part.strip())
            if not match:
                continue
            value, key = int(match.group(1)), match.group(2)
            if key == "failed":
                counts.failed = value
            elif key == "passed":
                counts.passed = value
            elif key in ("skipped", "todo"):
                counts.skipped += value
    return counts, items


# ---- JUnit XML（文件型）：pytest --junitxml / NUnit / jest-junit / ctest ----


def parse_junit_file(path: Path) -> tuple[Counts, list[tuple[str, str]]]:
    """读标准的 JUnit XML。这是跨语言覆盖率最广的一种"结果落文件"格式。"""
    counts = Counts()
    items: list[tuple[str, str]] = []
    root = ET.parse(str(path)).getroot()
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or "<unnamed>"
        test_id = f"{classname}::{name}" if classname else name
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            counts.failed += 1
            message = (failure.get("message") or failure.text or "failure").strip()
            items.append((test_id, message[:500]))
        elif error is not None:
            counts.errors += 1
            message = (error.get("message") or error.text or "error").strip()
            items.append((test_id, message[:500]))
        elif skipped is not None:
            counts.skipped += 1
        else:
            counts.passed += 1
    return counts, items


# 内置解析器注册。generic 必须始终存在 —— run_step 在遇到未注册名字时拿它兜底
for _name, _func, _kind_rules in (
    ("pytest", parse_pytest, None),
    ("unittest", parse_unittest, None),
    ("ctest", parse_ctest, None),
    ("generic", parse_generic, None),
    ("tap", parse_tap, None),
    ("gotest", parse_gotest, [
        ("code", re.compile(r"panic: ")),
        ("code", re.compile(r"^\s*\S+\.go:\d+:\d+:", re.M)),
    ]),
    ("cargo", parse_cargo, [
        ("code", re.compile(r"panicked at")),
        ("code", re.compile(r"^error\[E\d+\]", re.M)),
    ]),
    ("jest", parse_jest, None),
):
    register_parser(_name, _func, kind_rules=_kind_rules)

register_file_parser("junit", parse_junit_file)

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
    name = str(step.get("name") or step["command"][0])
    parser = step.get("parser", "generic")
    required = bool(step.get("required", STEP_REQUIRED_DEFAULT))
    # 单步超时可以覆盖 profile 级：一个慢的集成测试不该把整条流水线的超时顶宽
    limit = float(step.get("timeout") or timeout or 0)
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
        required=required,
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=limit or None,
        )
        output = _decode(proc.stdout)
        result.exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        result.exit_code = -9
        output = _decode(exc.stdout) + _decode(exc.stderr)
        output += f"\n[testflow] 步骤「{name}」超过 {limit:.0f}s 未结束，已中断"
    except FileNotFoundError:
        result.exit_code = 127
        output = f"[testflow] 找不到命令: {command[0]}"
    except OSError as exc:
        result.exit_code = 126
        output = f"[testflow] 无法执行命令: {exc}"
    result.duration_s = time.monotonic() - started
    result.output = output

    # 结果解析：文本型读 stdout，文件型读报告文件（JUnit XML 等）
    counts, items = Counts(), []
    file_func = PARSER_FILE_FUNCS.get(parser)
    report_rel = step.get("report") or step.get("junit")
    if file_func and report_rel:
        report_path = Path(str(report_rel))
        if not report_path.is_absolute():
            report_path = root / report_path
        if report_path.is_file():
            try:
                counts, items = file_func(report_path)
            except (ET.ParseError, OSError, ValueError) as exc:
                result.exit_code = result.exit_code or 1
                result.failures.append(
                    Failure(step=name, test="<report>", kind="unknown",
                            message=f"测试报告解析失败: {exc}"[:500])
                )
        else:
            # 约定了报告格式却没产出报告：多半是命令参数写错（如漏了 --junitxml）
            result.exit_code = result.exit_code or 1
            result.failures.append(
                Failure(step=name, test="<report>", kind="env",
                        message=f"未生成测试报告文件: {report_rel}")
            )
    else:
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
            message = f"步骤「{name}」超过 {limit:.0f}s 未结束"
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
            # 用步骤名当标识：报告里一眼看出是哪一步挂了，基线对比也不会串味
            Failure(step=name, test=name, kind=kind, message=message)
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


# ---------------------------------------------------------------- 预检 / 覆盖率 / 清单 / 基线
#
# 这四层是"常见测试方案"里 testflow v1 没覆盖的部分：环境没自检、覆盖率没门槛、
# 套件有没有被完整收集没人管、跟上一轮比是变好还是变坏也没人算。


def _rel(root: Path, path: Path) -> str:
    """能相对就相对 —— 报告可能被贴进 issue，别带本机绝对路径。"""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return path.name


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(text) or "")[:3]
    return tuple(int(p) for p in parts) if parts else (0,)


def run_preflight(preflight: Optional[dict], root: Path, env: dict) -> list[CheckResult]:
    """跑 ``[preflight]`` 里的环境自检。类型：python / exe / module / path / env。

    预检不过就不该跑测试 —— 环境不对时一屏 red 全是同一个原因，白等。
    """
    if not preflight:
        return []
    # 兼容两种写法：[preflight] 直接给数组，或 [preflight.checks] 数组
    checks = preflight.get("checks") if isinstance(preflight, dict) else preflight
    results: list[CheckResult] = []
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("type") or "").lower()
        name = str(check.get("name") or check.get("value") or kind)
        if kind == "python":
            minimum = str(check.get("min") or check.get("min_version") or "3.8")
            current = "%d.%d.%d" % sys.version_info[:3]
            ok = _version_tuple(current) >= _version_tuple(minimum)
            results.append(CheckResult(
                name, ok, "env", f"Python {current}（要求 >= {minimum}）",
                "" if ok else f"换解释器，或把 min 改成不大于 {current} 的版本",
            ))
        elif kind == "exe":
            target = str(check.get("value") or check.get("target") or check.get("name") or "")
            found = shutil.which(target, path=env.get("PATH"))
            detail = f"PATH 里{'找到' if found else '找不到'} {Path(found).name if found else target}"
            results.append(CheckResult(
                name, bool(found), "env", detail,
                "" if found else f"安装 {target} 或把它加进 PATH 后重跑",
            ))
        elif kind == "module":
            target = str(check.get("value") or check.get("target") or check.get("name") or "")
            found = importlib.util.find_spec(target) is not None if target else False
            results.append(CheckResult(
                name, found, "env", f"模块 {'已安装' if found else '缺失'}: {target}",
                "" if found else f"补依赖：pip install 对应包（{target}）",
            ))
        elif kind == "path":
            target = str(check.get("value") or "")
            exists = bool(target) and (root / target).exists()
            results.append(CheckResult(
                name, exists, "env", f"路径{'存在' if exists else '不存在'}: {target}",
                "" if exists else f"准备 {target}，或把它从 [preflight] 里去掉",
            ))
        elif kind == "env":
            target = str(check.get("value") or check.get("target") or check.get("name") or "")
            present = bool(env.get(target))
            results.append(CheckResult(
                name, present, "env",
                f"环境变量 {'已设置' if present else '未设置'}: {target}",
                "" if present else f"设置环境变量 {target} 后重跑",
            ))
        else:
            results.append(CheckResult(name, False, "env", f"未知的检查类型: {kind}",
                                       "type 只支持 python / exe / module / path / env"))
    # 把 required 从配置搬到结果上（顺序与有效 check 一一对应）
    flags = [bool(c.get("required", True)) for c in (checks or []) if isinstance(c, dict)]
    for result, flag in zip(results, flags):
        result.required = flag
    return results


def parse_coverage_report(path: Path) -> Optional[CoverageResult]:
    """识别三种最通用的覆盖率报告格式，别把工具绑死在某一个语言生态里。

    - coverage.py ``coverage json``：``totals.percent_covered``
    - istanbul ``json-summary``：``total.lines.pct``
    - Cobertura XML（C# / Java / pytest-cov 都能出）：``line-rate``
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        totals = data.get("totals") or {}
        if "percent_covered" in totals:
            return CoverageResult(
                percent=float(totals.get("percent_covered") or 0),
                covered=int(totals.get("covered_lines") or 0),
                total=int(totals.get("num_statements") or 0),
            )
        lines = (data.get("total") or {}).get("lines") or {}
        if "pct" in lines:
            return CoverageResult(
                percent=float(lines.get("pct") or 0),
                covered=int(lines.get("covered") or 0),
                total=int(lines.get("total") or 0),
            )
        return None
    if suffix == ".xml":
        try:
            root = ET.parse(str(path)).getroot()
        except (ET.ParseError, OSError):
            return None
        rate = root.get("line-rate")
        if rate is not None:
            try:
                return CoverageResult(percent=float(rate) * 100.0)
            except ValueError:
                return None
        valid, covered = root.get("lines-valid"), root.get("lines-covered")
        if valid and covered:
            try:
                return CoverageResult(
                    percent=100.0 * float(covered) / float(valid),
                    covered=int(covered),
                    total=int(valid),
                )
            except (ValueError, ZeroDivisionError):
                return None
    return None


def check_coverage(cfg: dict, root: Path) -> Optional[CoverageResult]:
    """按 ``[coverage]`` 的门槛判定。报告不存在就返回 None（不制造假失败）。"""
    rel = str(cfg.get("report") or cfg.get("file") or cfg.get("path") or "coverage.xml")
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return None
    result = parse_coverage_report(path)
    if result is None:
        return None
    result.report = _rel(root, path)
    result.min_percent = float(cfg.get("min", 0) or 0)

    floors = cfg.get("files") or {}
    if floors and path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for filename, stats in (data.get("files") or {}).items():
            achieved = ((stats or {}).get("summary") or {}).get("percent_covered")
            if achieved is None:
                continue
            for prefix, floor in floors.items():
                if str(filename).replace("\\", "/").startswith(str(prefix).replace("\\", "/")):
                    if float(achieved) < float(floor):
                        result.below.append(
                            f"{filename}: {float(achieved):.1f}% < {float(floor):g}%"
                        )
    return result


_COLLECT_MODULE = re.compile(r"^<Module\s+(.+?)>$")
_COLLECT_CASE = re.compile(r"^<(?:Function|TestCaseFunction|Coroutine|Method|UnitTestCase)\b")


def run_inventory(cfg: dict, root: Path, env: dict, timeout: float) -> Optional[dict]:
    """枚举实际被收集到的用例，与磁盘上的测试文件对账。

    "文件在、但一个用例都没被收集"是最典型的静默漏测：文件名不匹配
    ``test_*.py``、类名不以 ``Test`` 开头、被 conftest 跳过。
    """
    if not cfg:
        return None
    # -o addopts= 把项目自己的 addopts 清掉：不然项目里的 ``-v`` 会让 --collect-only
    # 输出成树状（<Module>/<TestCaseFunction>），条目一多就没法按 :: 解析
    command = cfg.get("command") or [
        sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=",
    ]
    command = expand_command(command, root, env)
    result: dict[str, Any] = {
        "command": sanitize_command(command, root),
        "collected": 0,
        "files": 0,
        "orphans": [],
        "ok": True,
        "detail": "",
    }
    started = time.monotonic()
    limit = float(cfg.get("timeout") or timeout or 0)
    try:
        proc = subprocess.run(
            command, cwd=str(root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=limit or None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update(ok=False, detail=f"收集命令执行失败: {exc}")
        return result
    result["exit_code"] = proc.returncode
    result["duration_s"] = round(time.monotonic() - started, 2)
    output = _decode(proc.stdout)

    globs = cfg.get("globs") or ["tests/test_*.py", "tests/**/test_*.py"]
    disk: set[str] = set()
    for pattern in globs:
        for path in root.glob(str(pattern)):
            if path.is_file():
                disk.add(_rel(root, path))
    by_name: dict[str, str] = {}
    for rel_path in disk:
        by_name.setdefault(rel_path.rsplit("/", 1)[-1], rel_path)

    collected_files: set[str] = set()
    collected = 0
    current_module = ""
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith(("=", "-", "!", "#")):
            continue
        module = _COLLECT_MODULE.match(line)
        if module:
            current_module = module.group(1).strip()
            continue
        if not line.startswith("<") and "::" in line:
            filename = line.split("::", 1)[0].strip().replace("\\", "/")
            if filename:
                collected_files.add(filename)
                collected += 1
            continue
        if _COLLECT_CASE.match(line) and current_module:
            # 树状输出只有文件名，用基名回查磁盘上的相对路径
            collected_files.add(by_name.get(Path(current_module).name, current_module))
            collected += 1
    result["collected"] = collected
    result["files"] = len(disk)
    result["orphans"] = sorted(p for p in disk if p not in collected_files)
    if collected == 0:
        result.update(ok=False, detail="收集命令没有报出任何用例（命令写错，或套件为空）")
    return result


def load_baseline(root: Path, report_path: str) -> Optional[dict]:
    """上一轮的报告就是最自然的基线，不用另存一份。"""
    path = Path(report_path)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def compare_baseline(previous: dict, failures: Sequence[dict], duration: float) -> dict:
    """和上一轮比：新增失败 / 已修复 / 耗时漂移。这就是"回归测试"的判定依据。"""
    prev_ids = {str(item.get("test")) for item in (previous.get("failures") or [])}
    cur_ids = {str(item.get("test")) for item in failures}
    prev_duration = float(previous.get("duration_s") or 0)
    delta = ((duration - prev_duration) / prev_duration * 100.0) if prev_duration else 0.0
    return {
        "previous_verdict": previous.get("verdict"),
        "previous_at": previous.get("started_at"),
        "new_failures": sorted(cur_ids - prev_ids),
        "fixed": sorted(prev_ids - cur_ids),
        "duration_delta_pct": round(delta, 1),
    }


def write_junit(root: Path, rel: str, report: dict) -> Path:
    """把结论写成 JUnit XML —— CI（Actions / Jenkins / GitLab）都能直接渲染。"""
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = report.get("counts") or {}
    project = str(report.get("project") or "testflow")
    profile = str(report.get("profile") or "default")
    suites = ET.Element("testsuites", {"name": project})
    suite = ET.SubElement(suites, "testsuite", {
        "name": f"{project}/{profile}",
        "tests": str(report.get("planned_steps") or len(report.get("steps") or [])),
        "failures": str(int(counts.get("failed", 0)) + int(counts.get("errors", 0))),
        "skipped": str(int(counts.get("skipped", 0))),
        "time": str(report.get("duration_s") or 0),
    })
    for step in report.get("steps") or []:
        case = ET.SubElement(suite, "testcase", {
            "classname": f"{project}/{profile}",
            "name": str(step.get("name")),
            "time": str(step.get("duration_s") or 0),
        })
        if step.get("exit_code"):
            details = step.get("failures") or []
            failure = ET.SubElement(case, "failure", {
                "message": str((details[0] if details else {}).get("message", "step failed"))[:300],
            })
            failure.text = "\n".join(
                f"[{item.get('kind')}] {item.get('test')}: {item.get('message')}"
                for item in details
            )
    ET.ElementTree(suites).write(str(path), encoding="utf-8", xml_declaration=True)
    return path


def failed_ids(results: Sequence[StepResult]) -> set[str]:
    return {failure.test for result in results for failure in result.failures}


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

    preflight = report.get("preflight") or []
    if preflight:
        bad = [check for check in preflight if not check["ok"]]
        print(f"预检：{len(preflight) - len(bad)}/{len(preflight)} 通过")
        for check in bad:
            flag = "" if check.get("required", True) else "（可选，不影响结论）"
            print(f"  · [{check['kind']}] {check['name']}{flag} — {check['detail']}")
            print(f"    -> {check['next']}")
    audits = report.get("audits") or []
    if audits:
        print(f"审计项（默认不计入结论，--strict 才算）：{len(audits)} 项未通过")
        for audit in audits:
            print(f"  · {audit['name']}  exit={audit['exit_code']}")
    coverage = report.get("coverage")
    if coverage:
        state = "达标" if coverage["ok"] else "不达标"
        print(
            f"覆盖率：{coverage['percent']:.1f}%"
            f"（门槛 {coverage['min_percent']:g}%，{state}）"
        )
        for item in (coverage.get("below") or [])[:5]:
            print(f"  · {item}")
    inventory = report.get("inventory")
    if inventory:
        print(
            f"套件清单：磁盘 {inventory.get('files', 0)} 个测试文件"
            f" / 收集到 {inventory.get('collected', 0)} 个用例"
        )
        orphans = inventory.get("orphans") or []
        if orphans:
            print(f"  ! 有测试文件但没收集到用例：{', '.join(orphans[:8])}")
        if not inventory.get("ok", True) and inventory.get("detail"):
            print(f"  ! {inventory['detail']}")
    baseline = report.get("baseline")
    if baseline:
        print(
            f"与上一轮（{baseline.get('previous_verdict')}）比："
            f"新增失败 {len(baseline.get('new_failures') or [])}"
            f" / 已修复 {len(baseline.get('fixed') or [])}"
            f" / 耗时 {(baseline.get('duration_delta_pct') or 0):+.1f}%"
        )
        for item in (baseline.get("new_failures") or [])[:5]:
            print(f"  · 新失败 {item}")
    flaky = report.get("flaky") or []
    if flaky:
        print(f"flaky（重复跑中时好时坏）：{', '.join(flaky[:8])}")

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
    # --profile 已经由上面按最终生效的 profile 拼好了，原样保留会重复一遍
    extras: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--profile":
            skip_next = True
            continue
        if arg in ("--json", "--quiet", "--no-clean", "--show-output",
                   "--strict", "--no-baseline", "--fail-on-regression"):
            continue
        extras.append(arg)
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
    parser.add_argument(
        "--strict", action="store_true",
        help="审计项（required=false）失败、漏测、覆盖率不达标也判 FAIL",
    )
    parser.add_argument(
        "--repeat", type=int, default=None,
        help="整条流水线重复跑 N 次，用来识别 flaky（时好时坏的用例）",
    )
    parser.add_argument("--junit", default=None, help="额外写一份 JUnit XML（给 CI 用）")
    parser.add_argument("--no-baseline", action="store_true", help="不与上一轮报告对比")
    parser.add_argument(
        "--fail-on-regression", action="store_true",
        help="与上一轮比出现新增失败即判 FAIL（其余情况只记录）",
    )
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
    strict = bool(args.strict or profile.get("strict"))
    repeat = max(1, int(args.repeat or profile.get("repeat") or 1))
    env = dict(os.environ)

    # 额外参数透传给最后一个步骤（通常是测试本身，而不是 build）
    if extra and steps:
        steps[-1] = {**steps[-1], "passthrough": True}

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.monotonic()

    # 1) 环境预检：不过关就不跑测试，否则一屏 red 全是同一个原因，白等
    preflight = run_preflight(profile.get("preflight"), root, env)
    preflight_failed = [check for check in preflight if not check.ok and check.required]

    # 2) 跑步骤。--repeat 时整条流水线重复 N 次，用来识别 flaky
    runs: list[list[StepResult]] = []
    changed_all: list[str] = []
    if not preflight_failed:
        for _round in range(repeat):
            before = snapshot(root, watch)
            round_results: list[StepResult] = []
            for step in steps:
                result = run_step(step, root, timeout, extra)
                round_results.append(result)
                # 前置步骤失败就不再往后跑：build 挂了还去 ctest 只会多一屏噪音。
                # 审计项默认不拦后续步骤 —— lint 红不该挡单元测试。
                should_stop = step.get("fail_fast", result.required)
                if result.exit_code != 0 and should_stop:
                    break
            runs.append(round_results)
            changed_all += diff_snapshots(before, snapshot(root, watch))

    # 多轮里取"最差的一轮"当主结果：重复跑的意义就是把偶发失败暴露出来
    primary: list[StepResult] = (
        max(runs, key=lambda items: sum(1 for r in items if r.exit_code != 0)) if runs else []
    )
    changed = sorted(set(changed_all))
    planned_required = sum(
        1 for step in steps if bool(step.get("required", STEP_REQUIRED_DEFAULT))
    )

    def gate(items: Sequence[StepResult]) -> list[StepResult]:
        return [r for r in items if r.required]

    if preflight_failed:
        verdict, reason = "FAIL", REASON_PREFLIGHT
    else:
        verdict, reason = decide_verdict(gate(primary), changed, planned_required)

    total = Counts()
    for result in primary:
        total.add(result.counts)
    failures = [f.to_dict() for result in primary for f in result.failures]

    # 审计项（required = false）：默认只记录，--strict 时才计入结论
    audits = [r for r in primary if not r.required and r.exit_code != 0]
    if audits and verdict == "PASS" and strict:
        verdict, reason = "FAIL", "audit-failed"

    # 3) 覆盖率门槛（读项目自己产出的标准报告，不替项目跑覆盖率工具）
    coverage = None
    coverage_cfg = profile.get("coverage")
    if coverage_cfg and not preflight_failed:
        coverage = check_coverage(coverage_cfg, root)
        if coverage is not None and not coverage.ok and verdict == "PASS":
            if bool(coverage_cfg.get("required", True)):
                verdict, reason = "FAIL", "coverage-below-min"

    # 4) 套件完整：文件在、用例一个没被收集 = 静默漏测
    inventory = None
    inventory_cfg = profile.get("inventory")
    if inventory_cfg and not preflight_failed:
        inventory = run_inventory(inventory_cfg, root, env, timeout)
        orphan_gate = bool(inventory_cfg.get("required") or strict)
        if inventory and inventory["orphans"] and orphan_gate and verdict == "PASS":
            verdict, reason = "FAIL", "orphan-tests"

    duration = time.monotonic() - started

    # 5) 与上一轮对比：新增失败 / 已修复 / 耗时漂移（上一轮报告就是基线）
    baseline = None
    if not args.no_baseline:
        previous = load_baseline(root, report_path)
        if previous:
            baseline = compare_baseline(previous, failures, duration)
            if args.fail_on_regression and baseline["new_failures"] and verdict == "PASS":
                verdict, reason = "FAIL", "regression"

    # 6) --repeat：在部分轮次失败、部分轮次通过的用例是 flaky
    flaky: list[str] = []
    runs_meta: list[dict] = []
    if repeat > 1:
        seen_in: dict[str, set[int]] = defaultdict(set)
        for index, round_results in enumerate(runs):
            for test_id in failed_ids(round_results):
                seen_in[test_id].add(index)
            run_counts = Counts()
            for result in round_results:
                run_counts.add(result.counts)
            run_verdict, _ = decide_verdict(gate(round_results), [], planned_required)
            runs_meta.append({
                "index": index + 1,
                "verdict": run_verdict,
                "counts": run_counts.to_dict(),
            })
        flaky = sorted(t for t, seen in seen_in.items() if 0 < len(seen) < repeat)

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
        "planned_required": planned_required,
        "steps": [r.to_dict() for r in primary],
        "counts": total.to_dict(),
        "failures": failures,
        "source_changed": changed,
        "artifacts_cleaned": [],
        "watch_patterns": watch,
        "summary": build_summary(
            verdict, reason, total, primary, duration,
            command_for_rerun(argv, args.profile, root),
        ),
        "report_path": report_path,
        # ---- v2 扩展（老调用方忽略即可）
        "preflight": [check.to_dict() for check in preflight],
        "audits": [r.to_dict() for r in audits],
        "coverage": coverage.to_dict() if coverage else None,
        "inventory": inventory,
        "baseline": baseline,
        "runs": runs_meta,
        "flaky": flaky,
        "strict": strict,
    }

    if not args.no_clean and artifacts:
        report["artifacts_cleaned"] = clean_artifacts(root, artifacts)

    # JUnit 要在写 JSON 之前生成：它读的就是这份 dict
    if args.junit:
        try:
            junit_path = write_junit(root, args.junit, report)
            report["junit_path"] = _rel(root, junit_path)
        except OSError as exc:
            print(f"[testflow] 写 JUnit 失败：{exc}", file=sys.stderr)

    path = write_report(root, report, report_path)
    report["report_path"] = str(path.relative_to(root)).replace("\\", "/") if path.is_relative_to(root) else str(path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.quiet:
        print(report["summary"])
    else:
        print_human(report, primary, args.show_output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
