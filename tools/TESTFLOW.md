# testflow —— 一条命令跑测试，只给三种结论

`tools/testflow.py` 是单文件、零第三方依赖的测试流水线，拷到任何项目都能用。
它把"跑完测试之后的一致性判断"固化成规则，**不需要每次靠人读输出临时决定**。

## 三种结论

| 结论 | 含义 | 退出码 |
|---|---|---|
| `PASS` | 所有步骤退出码 0，且运行期间被监视文件未变动 | 0 |
| `FAIL` | 有步骤失败（`reason` 说明是哪一类） | 1 |
| `INVALID` | 运行期间被监视文件有变动 —— **成败都不作数**，重跑 | 2 |

配置/工具自身出错是退出码 `3`，与"测试失败"区分开。

`reason` 细分：

| reason | 意思是 | 通常该做什么 |
|---|---|---|
| `ok` | 全绿 | 可以提交 |
| `tests-failed` | 跑完了，有用例失败 | 看 `failures[]` |
| `build-failed` | 前置步骤失败，后续没跑（C 项目 build 挂了） | 先修编译 |
| `timeout` | 某步骤超时 | 排查卡死点或调大 `timeout` |
| `command-error` | 退出码非 0 但解析不出用例（用法错、环境崩） | 看该步骤输出尾部 |
| `source-changed` | 跑的时候有人改了源码（就是 `INVALID`） | 重跑 |

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
python tools/testflow.py              # 跑默认 profile（ShellBreaker 就是 pytest 全量）
python tools/testflow.py -k eta       # 额外参数透传给最后一个步骤
python tools/testflow.py --profile lastfailed   # 只跑上次失败的
python tools/testflow.py --json       # 只输出 JSON，给程序/CI 读
python tools/testflow.py --quiet      # 只输出一行结论
python tools/testflow.py --no-clean   # 保留测试产物，便于事后排查
```

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

steps = [
  { name = "pytest", command = ["{python}", "-m", "pytest", "-q"], parser = "pytest" },
]
```

字段说明：

- `command` 是**数组**，不经 shell，跨平台安全。可用占位符 `{python}`、`{root}`、`{env:NAME}`。
- `parser` 可选 `pytest` / `unittest` / `ctest` / `generic`。`generic` 只看退出码。
- `fail_fast`（默认 `true`）：该步失败后不再跑后续步骤。
- `env`（可选）：给该步骤追加的环境变量。
- 最后一个步骤自动接收命令行里的额外参数（所以 `-k eta` 能透传给 pytest）。

### C 项目示例

```toml
[profiles.default]
watch = ["src/**/*.c", "include/**/*.h", "tests/**/*.c"]
artifacts = ["build/Testing"]
timeout = 900
steps = [
  { name = "build", command = ["cmake", "--build", "build", "-j"], parser = "generic" },
  { name = "ctest", command = ["ctest", "--test-dir", "build", "--output-on-failure"], parser = "ctest" },
]
```

build 失败时不会去跑 ctest，结论是 `FAIL / build-failed`，`kind=code` 并指出编译错误文件行。

## 移植到新项目

1. 把 `tools/testflow.py` 拷过去（不改一行代码）。
2. 写 `.testflow.toml`：`steps` 是你的测试命令，`watch` 是你的源码目录。
3. `python tools/testflow.py`。

`watch` 的作用是防"边跑边改"制造假失败：静态断言、快照对比类测试在源码变动时会读到
半新半旧的内容，结论不可信。没有这种测试的项目可以不配 `watch`（则不做快照）。

## 加一门新语言（不动核心）

三态判定、失败归类、报告格式都是语言无关的。新语言只需要一个**输出解析器**：

```python
# tools/testflow_go.py —— 以 go test 为例
import re

from testflow import Counts, register_parser


def parse_go(text: str, exit_code: int) -> tuple[Counts, list[tuple[str, str]]]:
    counts = Counts()
    for line in text.splitlines():
        if line.startswith("--- PASS:"):
            counts.passed += 1
        elif line.startswith("--- FAIL:"):
            counts.failed += 1
    items = [
        (line.split(":", 1)[1].strip(), "go: FAIL")
        for line in text.splitlines()
        if line.startswith("--- FAIL:")
    ]
    return counts, items


# kind_rules 是这个语言特有的措辞：通用规则认不出 "panic: "，会判成 unknown
register_parser("go", parse_go, kind_rules=[("code", re.compile(r"panic: "))])
```

配置里写 `parser = "go"` 即可。

**解析器契约**

- 入参 `(text, exit_code)`；`text` 是 stdout 与 stderr 合并后的文本
- 返回 `(Counts, [(用例名, 失败消息), ...])`
- **拿不到计数就返回空 `Counts()`，不要编数字**：退出码非 0 且没有失败条目时，
  testflow 会自己补一条带正确 `kind` 的记录（超时 / 命令不存在 / 按输出归类）
- 同名重复注册会覆盖，项目内要定制某个内置解析器直接 `register_parser` 覆盖即可

**已经预留的扩展位**

| 扩展点 | 位置 | 用途 |
|---|---|---|
| 解析器注册表 | `register_parser()` | 新语言/新框架的输出格式 |
| 语言特有归类 | `kind_rules=[(kind, regex)]` | `panic:`、`panicked at` 等本语言措辞 |
| 多步骤流水 | `steps` + `fail_fast` | build → lint → test 任意组合 |
| 占位符 | `{python}` `{root}` `{env:NAME}` | 不同项目的工具链差异 |
| 配置来源 | `.testflow.toml` / `.testflow.json` | 无 `tomllib` 的老版本 Python |

目前内置：`pytest` / `unittest` / `ctest` / `generic`。

## 约定（给人和 AI 都适用）

- **只看 `summary` 一行和 `failures[].next` 就能行动**，不需要读原始输出。
  需要细节时再 `--show-output` 或看报告里的字段。
- `verdict=INVALID` 时**不要试图解释失败原因**——先重跑，源码变了什么都说明不了。
- `kind=test` 不等于"改测试"，它只表示断言不成立。
- 提交前跑一次；`PASS` 才提交。这是唯一需要记住的规矩。
