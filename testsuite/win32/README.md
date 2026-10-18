# cgdb Windows 测试套件

在 ConPTY 里运行真实的 `cgdb.exe`，用 VT 按键驱动、屏幕解析断言，
覆盖 Windows 移植的全部使用路径。

## 运行（仓库根目录，Git Bash）

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"    # gcc：编译测试用的被调试程序
mingw32-make -f win32/Makefile            # 先构建 cgdb.exe
python testsuite/win32/run_tests.py
```

退出码 0 = 全部通过。总时长约 3~5 分钟（每个用例独立起一个 cgdb + gdb 会话）。

## 前置条件

- `.tmp/win-build/cgdb.exe` 已构建（`mingw32-make -f win32/Makefile`）
- `D:\Dev\mingw32\bin\gdb.exe` 存在
- PATH 里有 mingw32 的 gcc（用来现场编译测试 inferior，产物放
  `.tmp/win-tests/`）

## 覆盖矩阵

| 模块 | 覆盖 |
|---|---|
| cases_cli | `--version`/`--help`、`-d` 指向不存在路径、PATH 里没有 gdb（错误可见化 + 不崩溃的回归） |
| cases_session | 启动画面（源码窗/行号/状态条/提示符）、ESC/i 焦点切换（行为验证）、**状态条 `:` 后静置（KEY_UP 幽灵信号崩溃的回归用例）**、状态条退格/未知命令/ESC 取消、连发按键 |
| cases_debug | b main + run + 停止、inferior 输出显示、typed next / F10 / cont、空格切换断点、`u` until、^C 中断后 gdb 应答、无调试信息程序 |
| cases_nav | 源码窗 G/gg/Ctrl-D/Ctrl-U/j/k、`/` 搜索、GDB 窗行编辑（退格/Ctrl-U/Ctrl-A/E/F）、方向键历史、PageUp 滚动模式 |
| cases_windows | `+`/`-` 分屏调整（状态条行位置变化断言）、极限缩小、ConPTY resize（扩/缩/极小终端） |
| cases_files | CRLF+CJK 源文件显示与调试、窄终端行宽 |
| cases_commands | `:set`（syntax/winminheight/空值）、未知命令、`:help`、`:q`（空闲后/调试会话后/inferior 运行中） |

## 结构

- `wtlib.py` —— 基础设施：ConPTY 会话、VT100 屏幕解析器、
  check/summary 记账、测试 inferior 的现场编译
- `cases_*.py` —— 用例模块，每个 `register()` 返回用例函数列表
- `run_tests.py` —— 入口

新增用例：在某个 `cases_*.py` 里加函数（参数 `exes`，内部用
`wtlib.check(name, ok)` 断言），加进 `register()` 返回值即可。

## 已知约定

- 断言尽量用「文本出现/进程存活/退出码」这类稳定信号，不做像素级比对；
- 按键用 `s.keys()`（带间隔逐字节发送）模拟真实打字节奏（每个按键都会
  产生 key-up 记录——这正是踩出状态条崩溃 bug 的路径）；
- 每个用例独立起会话、独立清理，单个用例失败不影响后续执行。
