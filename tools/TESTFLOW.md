# testflow —— 一条命令跑测试，结论只有三种

`testflow.py` 是单文件、零第三方依赖的测试流水线，拷到任何项目都能用。
它把"跑完测试之后的一致性判断"固化成规则，**不需要每次靠人读输出临时决定**；
同时把常见测试方案里最容易被跳过的那几层（环境、覆盖率、套件完整性、回归对比、稳定性）
做成声明式配置，而不是每个项目自己写一遍脚本。

两种用法：

```python
# 1) 命令行：项目根目录放一个 .testflow.toml 即可
#    python tools/testflow.py
# 2) 当库调用：参数仍是命令行那套
import testflow
code = testflow.main(["--quiet", "--profile", "ci"])
```

## 三种结论

| 结论 | 含义 | 退出码 |
|---|---|---|
| `PASS` | 所有必需步骤退出码 0，且运行期间被监视文件未变动 | 0 |
| `FAIL` | 有必需步骤失败（`reason` 说明是哪一类） | 1 |
| `INVALID` | 运行期间被监视文件有变动 —— **成败都不作数**，重跑 | 2 |

配置/工具自身出错是退出码 `3`，与"测试失败"区分开。

`reason` 细分：

| reason | 意思是 | 通常该做什么 |
|---|---|---|
| `ok` | 全绿 | 可以提交 |
| `tests-failed` | 跑完了，有用例失败 | 看 `failures[]` |
| `build-failed` | 必需的前置步骤失败，后续没跑（C 项目 build 挂了） | 先修编译 |
| `timeout` | 某步骤超时 | 排查卡死点或调大 `timeout` |
| `command-error` | 退出码非 0 但解析不出用例（用法错、环境崩） | 看该步骤输出尾部 |
| `source-changed` | 跑的时候有人改了源码（就是 `INVALID`） | 重跑 |
| `preflight-failed` | 环境预检没过，测试根本没跑 | 按 `preflight[].next` 补齐 |
| `coverage-below-min` | 覆盖率低于门槛 | 补测试或调门槛 |
| `orphan-tests` | 有测试文件一个用例都没被收集（静默漏测） | 改文件名/类名/收集配置 |
| `audit-failed` | 审计项（`required = false`）失败，且开了 `--strict` | 修 lint/类型检查 |
| `regression` | 比上一轮多出失败，且开了 `--fail-on-regression` | 看 `baseline.new_failures[]` |

## 判定分六层

一层不过，后面几层该跑还是跑，报告里全部列出来 —— 一次拿全信息，别让人来回跑。

| # | 层 | 规则 | 失败时 |
|---|---|---|---|
| 1 | 环境预检 `preflight` | Python 版本 / 可执行文件 / 依赖模块 / 路径 / 环境变量 | 不跑测试，`preflight-failed` |
| 2 | 用例结果 `steps` | 必需步骤全部退出码 0 → PASS | `tests-failed` 等 |
| 3 | 审计项 `required = false` | 默认只记录，`--strict` 才算失败 | `audit-failed` |
| 4 | 覆盖率 `coverage` | 读项目自己产出的报告，按 `min` 与逐文件门槛判定 | `coverage-below-min` |
| 5 | 套件完整 `inventory` | 磁盘上的 `test_*.py` 是否都被收集到用例 | 默认只提醒，`--strict` 判 `orphan-tests` |
| 6 | 基线对比（自动） | 与上一轮报告比：新增失败 / 已修复 / 耗时漂移 | 默认只记录，`--fail-on-regression` 判 `regression` |

`--repeat N` 会让整条流水线跑 N 次：**部分轮次失败、部分轮次通过的用例**会被列进
`flaky[]`，主结论取最差的那一轮 —— 偶发失败不该因为"这次恰好绿了"被放过去。

## 失败归类

每条失败都带 `kind` 和 `next`，不用再猜：

| kind | 判据 | `next` 的指引 |
|---|---|---|
| `env` | `ModuleNotFoundError` / 命令不存在 / 权限 / 路径不存在 | 补依赖、检查工具链 |
| `timeout` | `timed out` 或步骤被中断 | 排查卡死 |
| `code` | 源码异常（`TypeError`/`AttributeError`…）、编译错误 | 改实现 |
| `test` | `AssertionError` | **先确认实现是否符合预期**——可能是实现错，也可能测试期望过时 |
| `unknown` | 以上都不匹配 | 看原始输出尾部 |

注意 `AssertionError` 刻意归 `test` 而不是 `code`：它只说明"期望与实际不符"，
谁错需要人判断，`next` 里就是这么写的，别把它当结论用。

## 用法

```bash
python tools/testflow.py                          # 跑默认 profile
python tools/testflow.py -k eta                   # 额外参数透传给最后一个步骤
python tools/testflow.py --profile lastfailed     # 只跑上次失败的
python tools/testflow.py --json                   # 只输出 JSON，给程序/CI 读
python tools/testflow.py --quiet                  # 只输出一行结论
python tools/testflow.py --show-output            # 失败时附带输出尾部
python tools/testflow.py --no-clean               # 保留测试产物，便于事后排查
python tools/testflow.py --strict                 # 审计项/漏测/覆盖率不达标也判 FAIL
python tools/testflow.py --repeat 3               # 跑三遍，识别 flaky
python tools/testflow.py --fail-on-regression     # 新增失败即判 FAIL
python tools/testflow.py --junit out/junit.xml    # 顺手写一份 JUnit XML 给 CI
```

| 参数 | 作用 |
|---|---|
| `--config PATH` | 指定配置文件（默认自动找 `.testflow.toml` / `.testflow.json`） |
| `--profile NAME` | 用哪个 profile（默认 `default`） |
| `--timeout SEC` | 覆盖配置里的单步超时 |
| `--json` / `--quiet` | 机器可读 / 只一行结论 |
| `--strict` | 把"只提醒"的层升级成硬失败：审计项、漏测、覆盖率、预检可选项 |
| `--repeat N` | 整条流水线重复 N 次，输出 `runs[]` 与 `flaky[]` |
| `--junit PATH` | 按步骤写 JUnit XML |
| `--no-baseline` | 不与上一轮报告对比 |
| `--fail-on-regression` | 出现新增失败时判 FAIL |

`--quiet` 的输出长这样，够用来判断要不要继续：

```
[FAIL] tests-failed · 2 failed / 254 passed · 148.1s · test×2 · 重跑: python tools/testflow.py
```

报告写到 `.testflow/last-run.json`，摘要追加到 `.testflow/history.jsonl`（保留 200 条）。
报告里的路径一律相对仓库根、`{python}` 会还原成占位符 —— 可以直接贴进 issue。

## 配置

默认找 `.testflow.toml`（Python 3.11+ 自带 `tomllib`；3.10 需 `pip install tomli`
或改用同名 `.testflow.json`）。

```toml
[project]
name = "项目名"                       # 只用于报告显示
report = ".testflow/last-run.json"    # 报告路径（可省）

[profiles.default]
watch = ["src/**/*.py", "tests/**/*.py"]   # 参与"源码是否被改"快照的 glob
artifacts = ["logs/junk.jsonl"]            # 跑完清理（只删文件与空目录，越界拒绝）
timeout = 900                              # 单步超时秒数（可被 --timeout 覆盖）
repeat = 1                                 # 等价于 --repeat
strict = false                             # 等价于 --strict

steps = [
  { name = "pytest", command = ["{python}", "-m", "pytest", "-q"], parser = "pytest" },
]
```

字段说明：

- `command` 是**数组**，不经 shell，跨平台安全。可用占位符 `{python}`、`{root}`、`{env:NAME}`。
- `parser` 可选 `pytest` / `unittest` / `ctest` / `tap` / `gotest` / `cargo` / `jest` /
  `junit`（文件型）/ `generic`。`generic` 只看退出码。
- `required`（默认 `true`）：设成 `false` 就是**审计项**（lint、类型检查、格式检查），
  失败只记录，不影响结论；`--strict` 时才判 FAIL。
- `fail_fast`（默认 = `required`）：该步失败后不再跑后续步骤。审计项失败不会拦后续步骤。
- `timeout`（可选）：该步骤单独的超时。
- `env`（可选）：给该步骤追加的环境变量。
- `report` / `junit`（文件型解析器用）：报告文件路径，相对仓库根。
- 最后一个步骤自动接收命令行里的额外参数（所以 `-k eta` 能透传给 pytest）。

### 环境预检

```toml
preflight = [
  { type = "python", min = "3.9",  name = "Python 3.9+" },
  { type = "module", value = "pytest", name = "pytest" },
  { type = "module", value = "PySide6", name = "PySide6", required = false },
  { type = "exe",    value = "ffmpeg", name = "ffmpeg" },
  { type = "path",   value = "tests/fixtures", name = "测试素材" },
  { type = "env",    value = "FFMPEG_PATH", name = "FFMPEG_PATH" },
]
```

也可以写成 `[preflight.checks]` 表数组，两种写法等价。
预检不过就**不跑测试**：环境不对时一屏 red 全是同一个原因，白等一轮。

### 覆盖率门槛

```toml
[profiles.default.coverage]
report = ".testflow/coverage.json"   # coverage.py 的 json / Cobertura xml；也可用 file / path
min = 80                             # 全局门槛（百分比）
required = true                      # false 则只记录
[profiles.default.coverage.files]    # 逐文件更严（只支持 json 报告）
"core/" = 90
```

生成报告是项目自己那一步的事（`coverage run -m pytest`、`jest --coverage`、
`dotnet test --collect`），testflow 只负责**按门槛判定**，不替项目装依赖。
报告不存在就跳过，不制造假失败。

### 套件完整（静默漏测）

```toml
inventory = { timeout = 120 }
```

默认用 `pytest --collect-only -q -o addopts=` 数一遍用例，再和磁盘上
`tests/test_*.py` 对账：**文件在、用例一个都没被收集**是最典型的漏测
（文件名不匹配、类名不是 `Test` 开头、被 `conftest` 跳过）。

- `globs`：对账的文件范围，默认 `["tests/test_*.py", "tests/**/test_*.py"]`
- `command`：自定义收集命令
- `required = true`：漏测直接判 FAIL（不写则只提醒，`--strict` 时判 FAIL）

### 其它语言的步骤

```toml
# C
steps = [
  { name = "build", command = ["cmake", "--build", "build", "-j"], parser = "generic" },
  { name = "ctest", command = ["ctest", "--test-dir", "build", "--output-on-failure"], parser = "ctest" },
]

# Node（node --test 输出 TAP / Jest 文本）
steps = [
  { name = "lint", command = ["npx", "eslint", "."], parser = "generic", required = false },
  { name = "test", command = ["npx", "jest", "--ci"], parser = "jest" },
]

# Go / Rust
steps = [
  { name = "gotest", command = ["go", "test", "-v", "./..."], parser = "gotest" },
  { name = "cargo",  command = ["cargo", "test", "--", "--nocapture"], parser = "cargo" },
]

# 已有 JUnit XML 产物的框架（xunit / MSTest / vitest junit reporter…）
steps = [
  { name = "xunit", command = ["dotnet", "test", "--logger", "trx"], parser = "junit",
    report = "TestResults/junit.xml" },
]
```

## 报告字段

`last-run.json` 的老字段没变，v2 只**追加**：

| 字段 | 说明 |
|---|---|
| `verdict` / `reason` / `exit_code` | 三态结论 |
| `steps[]` | 每步：`name`、`exit_code`、`counts`、`failures[]`、`timed_out`、`required` |
| `counts` | 汇总 `total/passed/failed/errors/skipped` |
| `failures[]` | `kind` + `next` + 出处步骤 |
| `source_changed` | 跑测试期间被改动的文件 |
| `preflight[]` | 环境预检明细（`ok`、`required`、`next`） |
| `audits[]` | 失败但不算结论的审计项 |
| `coverage` | `percent`、`min_percent`、`below[]`、`ok` |
| `inventory` | `collected`、`files`、`orphans[]` |
| `baseline` | `new_failures[]`、`fixed[]`、`duration_delta_pct` |
| `runs[]` / `flaky[]` | `--repeat` 的每轮结论与偶发失败用例 |

## 移植到新项目

1. 把 `testflow.py` 拷过去（不改一行代码）。
2. 写 `.testflow.toml`：`steps` 是你的测试命令，`watch` 是你的源码目录。
3. `python tools/testflow.py`。

`watch` 的作用是防"边跑边改"制造假失败：静态断言、快照对比类测试在源码变动时会读到
半新半旧的内容，结论不可信。没有这种测试的项目可以不配 `watch`（则不做快照）。

## 加一门新语言（不动核心）

三态判定、失败归类、报告格式都是语言无关的。新语言只需要一个**输出解析器**：

```python
# tools/testflow_mylang.py —— 以某种自研框架为例
import re

from testflow import Counts, register_parser


def parse_mylang(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    for line in text.splitlines():
        if line.startswith("ok   "):
            counts.passed += 1
        elif line.startswith("FAIL "):
            counts.failed += 1
    items = [
        (line.split(" ", 1)[1].strip(), "mylang: FAIL")
        for line in text.splitlines()
        if line.startswith("FAIL ")
    ]
    return counts, items


# kind_rules 是这个语言特有的措辞：通用规则认不出，会判成 unknown
register_parser("mylang", parse_mylang, kind_rules=[("code", re.compile(r"^panic: "))])
```

配置里写 `parser = "mylang"` 即可。已经有现成报告文件的框架可以用**文件型解析器**：

```python
from testflow import register_file_parser
register_file_parser("surefire", lambda path: parse_xml_like(path))
```

**解析器契约**

- 入参 `(text, exit_code)`；`text` 是 stdout 与 stderr 合并后的文本
- 返回 `(Counts, [(用例名, 失败消息), ...])`
- **拿不到计数就返回空 `Counts()`，不要编数字**：退出码非 0 且没有失败条目时，
  testflow 会自己补一条带正确 `kind` 的记录（超时 / 命令不存在 / 按输出归类）
- 同名重复注册会覆盖，项目内要定制某个内置解析器直接 `register_parser` 覆盖即可

**已经预留的扩展位**

| 扩展点 | 位置 | 用途 |
|---|---|---|
| 解析器注册表 | `register_parser()` / `register_file_parser()` | 新语言/新框架的输出格式 |
| 语言特有归类 | `kind_rules=[(kind, regex)]` | `panic:`、`panicked at` 等本语言措辞 |
| 多步骤流水 | `steps` + `required` + `fail_fast` | build → lint → test 任意组合 |
| 判定层次 | `preflight` / `coverage` / `inventory` / `--strict` | 环境、覆盖率、漏测、审计 |
| 占位符 | `{python}` `{root}` `{env:NAME}` | 不同项目的工具链差异 |
| 配置来源 | `.testflow.toml` / `.testflow.json` | 无 `tomllib` 的老版本 Python |

目前内置——文本型：`pytest` / `unittest` / `ctest` / `tap` / `gotest` / `cargo` /
`jest` / `generic`；文件型：`junit`。

## 约定（给人和 AI 都适用）

- **只看 `summary` 一行和 `failures[].next` 就能行动**，不需要读原始输出。
  需要细节时再 `--show-output` 或看报告里的字段。
- `verdict=INVALID` 时**不要试图解释失败原因**——先重跑，源码变了什么都说明不了。
- `kind=test` 不等于"改测试"，它只表示断言不成立。
- `flaky[]` 非空说明**要么实现有竞态、要么测试自身依赖顺序/时间**，先修再谈覆盖率。
- 提交前跑一次；`PASS` 才提交。这是唯一需要记住的规矩。
