# CGDB Windows 移植可行性报告

本文档是移植决策的依据，所有关键论断都来自 2026-10-18 在本机（Windows 10 22H2,
build 19045）上的实测，复现脚本在 `.tmp/test_*.py`、`.tmp/swtest.c`、
`.tmp/conpty_run.py`。代码层面的模块分析见 `doc/reference.md`，工具链与构建命令见
仓库根 `AGENTS.md`。

- [1. 结论](#1-结论)
- [2. 系统基线：Windows 10 1809+](#2-系统基线windows-10-1809)
- [3. 实测记录](#3-实测记录)
- [4. 架构方案：MI-over-pipes + 合成控制台](#4-架构方案mi-over-pipes--合成控制台)
- [5. curses 后端选型](#5-curses-后端选型)
- [6. 自写 swin 渲染后端的可行性（兜底路线）](#6-自写-swin-渲染后端的可行性兜底路线)
- [7. 工作量分级](#7-工作量分级)
- [8. 风险清单](#8-风险清单)
- [9. 建议的移植阶段](#9-建议的移植阶段)

---

## 1. 结论

**cgdb 可以移植到 Windows，最低系统基线定为 Windows 10 1809（build 17763，
2018-10）**。约七成代码可原样或小改复用；唯一必须重新设计的是「GDB 进程通道」
这一层——原架构的 `new-ui` 双通道在原生 Windows GDB 上不可用（实测致命），
替代方案 MI-over-pipes 已实测畅通，且是 VS Code / CLion 等 Windows 前端的
成熟做法。

三大支柱的实测状态：

| 支柱 | 状态 | 证据 |
|---|---|---|
| GDB 交互（MI 通道） | ✅ 实测通过 | `gdb --interpreter=mi` 走 stdin/stdout 管道，`-break-info` 返回完整 `^done` 记录（3.4 节） |
| GDB 控制台复现 | ⚠️ 需换实现 | `new-ui` 第二通道在 Windows GDB 上会触发进程退出（3.2 节）；GDB 本体在 ConPTY 里交互完全正常 |
| curses UI 渲染 | ✅ 实测通过 | PDCurses-wincon：真彩 + Unicode 框线 + CJK + 控制台 API 码页免疫（5 节） |

## 2. 系统基线：Windows 10 1809+

移植版需要的 Windows 能力与最低版本：

| 能力 | 用途 | 最低版本 |
|---|---|---|
| ConPTY（`CreatePseudoConsole`） | 被调试程序（inferior）的独立终端 | **Win10 1809 (build 17763)** |
| VT 输出（`ENABLE_VIRTUAL_TERMINAL_PROCESSING`） | 自写渲染后端的 VT 直出路线 / PDCurses-vt | Win10 1607 |
| 命名管道、`WaitForMultipleObjects`、`ReadConsoleInput` | 事件循环与输入 | 古董级，无约束 |

「最低 Windows 10」因此必须明确为 **1809 及以上**：1507~1803 没有 ConPTY，
被调试程序的独立终端做不了（可做降级模式但不值得）。仍在服役的 Windows 10
几乎全部 ≥ 1909，这个基线没有实际覆盖率损失。全部实验在 19045 上通过。

顺带的一条 Windows 编码事实（影响所有渲染路线）：**字节级写控制台
（`WriteFile`）会经过控制台代码页转换**，本机默认 CP936（GBK）下，UTF-8 字节
和 CP437 框线字节都会错码、甚至 ESC 字节被吞。绕过方式只有两种：宽字符 API
（`WriteConsoleOutputW` 等，ncursesw 的做法）或先把控制台输出码页设为 65001
（`SetConsoleOutputCP(CP_UTF8)`，PDCurses-vt 路线需要，但实测该库在 CP65001
下崩溃，见 5 节）。

## 3. 实测记录

测试对象：`D:\Dev\mingw32\bin\gdb.exe`（GDB 17.1，i686 原生 mingw 构建；
`D:\Linux\mingw64` 的 64 位 GDB 同样测过）。系统无其他 gdb 干扰。

### 3.1 GDB 在 ConPTY 里交互正常

`gdb --nw -q` 挂在 ConPTY 上（`CreatePseudoConsole` + 正确的 STARTUPINFO 接线）：
常驻 `(gdb)` 提示符不退出，向 ConPTY 输入管道写 `print 40+2\r`，控制台输出流
里出现完整的 readline 行编辑 VT 序列和 `$1 = 42`。cmd.exe 同样正常交互。
**GDB 的 readline 在伪控制台里完全可用**。

ConPTY 接线配方（官方样例没写全、实战踩出来的坑）：

1. `CreatePseudoConsole(COORD按值, 输入管道读端, 输出管道写端, 0, &hPC)`
   —— ctypes 不设 argtypes 时 COORD 会按指针传，必须显式声明；
2. `UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
   hPC句柄值, sizeof(HPCON), NULL, NULL)` —— 传句柄值本身，**不是 byref**；
3. **`STARTF_USESTDHANDLES` + 三个 hStd 句柄全 NULL** —— 否则子进程按值继承
   宿主的标准句柄（若宿主句柄可继承，输出直接漏进宿主的 stdout，ConPTY 管道
   收不到任何东西）；
4. `CreateProcessW(..., FALSE, EXTENDED_STARTUPINFO_PRESENT, ...)`；
5. CreateProcess 后再关 ConPTY 侧的两个管道端；
6. `ClosePseudoConsole` 可能阻塞（要排空输出通道），放在后台线程带超时调用。

### 3.2 致命发现：`new-ui` 在原生 Windows GDB 上不可用

cgdb 在 Linux 上用 `gdb --nw -ex "new-ui mi /dev/pts/N"` 开出第二个 UI 通道。
在 Windows GDB 17.1 上实测：

| `new-ui mi` 的目标 | 结果 |
|---|---|
| 命名管道 `\\.\pipe\...` | GDB 能打开管道、写出 MI banner（`=thread-group-added...`），随后**自行退出 (exit 0)** |
| 普通文件（预创建） | 同样写完 banner 后退出 |
| `CON` | 直接报 Invalid argument |

32 位（D:\Dev\mingw32）和 64 位（D:\Linux\mingw64）的 GDB 17.1 行为一致。
结论：**GDB 注册第二个 UI 后，下一次事件循环迭代即失败退出——mingw 构建的
GDB 事件循环无法在 Windows 上多路复用非 console 通道，`new-ui` 整体不可用**。
值得将来去 GDB 上游求证/提 bug，但移植不能赌它被修好。

附带发现（如果哪天 new-ui 修好了会用到）：
- GDB 命令行参数解析器（buildargv）把 `\` 当转义符吃掉一半，
  `\\.\pipe\name` 要写成 `\\\\.\\pipe\\\\name` 或正斜杠 `//./pipe/name`；
- msvcrt 的 `open()` 本身能打开监听中的 `\\.\pipe\` 管道（CRT 层不是障碍）。

### 3.3 GDB 控制台输出直写宿主 stdout 的假象

排查过程中一度观察到「GDB 输出绕过 ConPTY 直接出现在宿主 stdout」——根因是
3.1 节配方第 3 条：宿主（本测试环境）的 stdout 是可继承的文件句柄，子进程按值
拿到就直接写了。这不是 GDB 的问题，是接线问题，修正配方后消失。

### 3.4 替代方案实测通过：MI over stdin/stdout 管道

```
gdb --nw -q --interpreter=mi   (stdin/stdout = 两条匿名管道)
```

- 启动即输出 `=thread-group-added,id="i1"\n(gdb) \n`；
- 写入 `-break-info\n` → 返回完整 `^done,BreakpointTable={...}`；
- 写入 `-gdb-exit\n` → 干净退出；
- 常驻提示符，无 new-ui 的死亡问题。

这是全部 Windows MI 前端的标准形态，GDB 对它的支持是第一优先级的。

### 3.5 curses 后端实测

测试程序 `swtest.c`（框线/彩色/256 色/下划线/CJK/子窗口），编译为
ncursesw、PDCurses-vt、PDCurses-wincon 三个后端，各自跑在 ConPTY 里，捕获
VT 字节流后用自写 VT100 屏幕解析器重建画面核对。结果见第 5 节。

## 4. 架构方案：MI-over-pipes + 合成控制台

```c
/* Linux 现状                              Windows 移植方案 */
gdb --nw -ex "new-ui mi /dev/pts/N"   →   gdb --nw -q --interpreter=mi
console pty (gdb 内 readline)         →   ✗ 取消，GDB 窗口由 cgdb 合成
MI pty (new-ui)                       →   stdin/stdout 两条匿名管道（实测✅）
inferior 独立 tty (-inferior-tty-set) →   ConPTY，输出喂给现成的 libvterm 渲染
select() 五路 fd                      →   WaitForMultipleObjects（控制台输入
                                          句柄官方文档确认可入等待集，或输入
                                          专用线程+事件）
SIGWINCH 自管道                       →   WINDOW_BUFFER_SIZE_EVENT（开
                                          ENABLE_WINDOW_INPUT）/ 事件对象
termios cbreak                        →   SetConsoleMode
kui 的终端转义序列匹配层              →   ReadConsoleInput 的 VK 码 →
                                          CGDB_KEY 直查表，整层绕过 termcap
```

**对现有代码的影响范围**：

- `lib/tgdb/tgdb.cpp`：命令队列、请求映射（`-break-info` 等全是 MI 命令）、
  gdbwire 回调链——**全部原样保留**。变化只在传输层：原来 console/MI 两个 fd
  的分发，变成单 MI 管道；`console_output` 回调改由 MI 的 console 流记录
  （`~"..."`）和 target 输出记录喂入。
- `lib/tgdb/gdbwire.c`：纯 C 零 POSIX，MI-over-pipes 正是它的设计用途，不动。
- `cgdb/scroller + vterminal`：下半屏从「GDB 会话的终端复现」变成「合成控制
  台」——cgdb 自己的行编辑器收键（kui 已提供全部按键基建），回车后翻译成
  `-interpreter-exec console "..."` 发 MI，输出从 console 流记录渲染进
  scroller。`(gdb)` 提示符由 cgdb 合成。**这是最大的一块新增工作**，
  代价是丢掉 GDB 自带 readline 的历史/补全。
- `tgdb_resize_console`（TIOCSWINSZ）→ MI 通道不再需要；inferior 的
  ConPTY 用 `ResizePseudoConsole`。
- inferior 输出：GDB 在 MI 模式下被调试程序的 stdio 默认继承 GDB 的管道，
  会混进 MI 流。归置方案（设计期决定）：ConPTY + gdb 的 `set new-console`
  /`start` 变体，或经 MI 命令重定向。GDB 窗口与 inferior 窗口的混排是
  UX 设计题，不是可行性障碍。

**信号语义映射**：Ctrl-C 仍可经 ConPTY 输入管道写 `\x03`（ConPTY 会把它翻译
成子进程的 CTRL_C_EVENT）或直接 `GenerateConsoleCtrlEvent`；GDB 退出检测用
管道 EOF（对应 Linux 上 pty master 的 EIO 语义，`io_read` 的 EIO=EOF 处理
逻辑可平移为「管道破裂」）。

## 5. curses 后端选型

实测矩阵（mingw32 工具链，ConPTY 30x100，CP936 系统locale）：

| 能力 | ncursesw 6.6<br>(win32 驱动构建) | PDCurses 4.4 wincon | PDCurses 4.4 vt |
|---|---|---|---|
| 初始化 / 尺寸探测 | ✅ 30x100 | ✅ 30x100 | ✅ |
| Unicode 框线（┌─┬│┘） | ✅ | ✅ | ❌ CP437 字节，GBK 码页下错码 |
| 基本属性 bold/rev/ul | ✅（91m/42m/4m） | ✅（91m/4m/24m） | ✅（但定位错乱） |
| 颜色数 | ❌ **8 色**（+bold=亮色 90-97 系） | ✅ **256 色板 → 24 位真彩输出**（`38;2;255;135;0`） | 真彩，但流损坏 |
| CJK | ✅ 宽字符 API（`addwstr`）；字节 API 需 UTF-8 locale（本机 setlocale 返回 CP936） | ✅ UTF-8 `addstr` 直出 | ❌ |
| 输出机制 | `WriteConsoleOutputW` 等宽字符 API——**码页免疫** | 控制台 API，码页免疫 | **字节级 VT 流，过控制台码页**（CP936 下 ESC 被吞） |
| 稳定性 | ✅ | ✅ | ❌ `SetConsoleOutputCP(65001)` 后崩溃（0xC0000005） |

关键判定：

1. **首选 PDCurses-wincon**（`-lpdcurses_wincon`，加 `-lwinmm -luser32`；
   头文件必须用根目录 `pdcurses.h` 包装——它映射构建后缀符号如
   `endwin_x64_4400` 并定义 PDC_WIDE/PDC_FORCE_UTF8；用 `pdcurses/curses.h`
   裸头会链接失败）：真彩、Unicode、CJK、码页免疫，全部实测通过。
2. **ncursesw 作备胎**：链接顺滑、win32 驱动构建稳定，但只有 8 色（16 有效），
   对应 `highlight_groups.cpp` 现成的 ANSI 降级路径。另注意它导入了
   `ReadConsoleInputW`（自带输入层，cgdb 不用，无妨）。
3. **PDCurses-vt 不用**：字节级输出在非 UTF-8 码页的控制台上必错码，
   改 UTF-8 码页又触发库崩溃。
4. 宽字符 DLL 组合（根头 + `-lpdcurses` 动态）未单独跑完，静态 wincon 已
   够用。

## 6. 自写 swin 渲染后端的可行性（兜底路线）

回答「如果 curses 都不行，自己写渲染行不行」：**行，而且是可控的有界工程**。
依据：

1. **`sys_win.h` 没有任何输入函数**（键入全部走 kui 自己读控制台）——自写
   后端只需要做：渲染、终端查询、颜色对、resize，约 40 个 API 中多数是一行
   转发。
2. 需要实现的窗口模型很朴素：每个 `SWINDOW` 一个单元格网格；
   `swin_wnoutrefresh` 把窗口网格合成到一张虚拟屏；`swin_doupdate` 做虚拟屏
   与物理屏的差分输出。这正是 curses 的核心，独立实现约 1000~1500 行。
3. 两条输出路线今天都已实测验证过可行性：
   - **win32 console API 路线**（`WriteConsoleOutputW`/CHAR_INFO 网格 +
     `SetConsoleTextAttribute`）：码页免疫、任意控制台可用（含老 conhost），
     ncursesw 的 win32 驱动就是这么干的（3.5 节实测其输出完美）。代价是
     16 色属性上限；
   - **VT 直出路线**（开 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`，自己发差分
     VT 序列）：真彩、与 ConPTY/Windows Terminal 天然合拍，PDCurses-vt 已
     证明这条路能出真彩（它的失败只在码页处理，自写后端全用宽字符→
     UTF-8 转换可避开）。要求 1607+。
4. PDCurses-wincon 的源码就是一份可参照的工作实现。
5. 结论排序：**PDCurses-wincon（零成本）→ ncursesw（8 色备胎）→ 自写 swin
   后端（保证能落地的兜底，优先 console API 起步、需要真彩再加 VT 路线）**。

另一个概念澄清：ConPTY 与 cgdb 自身 UI 无关——cgdb 的 UI 渲染到它所在的
真实控制台；ConPTY 只用于被调试程序的终端（对应 Linux 上的 inferior-tty）。
「用 ConPTY 渲染自己 UI」不需要也不该做。

## 7. 工作量分级

| 级别 | 内容 |
|---|---|
| 直接编译 | gdbwire.c、libvterm、kui 的 tree/ctx/map_set/manager、tokenizer 的 flex 生成物、UI 业务逻辑（经 swin 层） |
| 小时级适配 | `strcasecmp`→`_stricmp`、fs_util 路径、highlight.cpp 的 POSIX regex、kui_term 的 VK 直查表、rline 整体 stub、clog 的 32 位 printf 格式 |
| 中等重写（lib/util 平台层） | pseudo/fork_util → ConPTY+CreateProcess 适配层、io.cpp → 句柄读写与等待、terminal.cpp → SetConsoleMode、主循环 → WaitForMultipleObjects、独立构建脚本 |
| 大块新增 | GDB 窗口行编辑器与命令翻译层（第 4 节）；inferior 终端窗口与输出归置 |

## 8. 风险清单

1. **行编辑器体验上限**：丢掉 GDB readline 的历史/补全，需要自己实现基础版
   （cgdb 的 `:`/`/` 命令行编辑已有可复用的原语）；
2. **inferior 输出归置**：`-inferior-tty-set` 需要 tty 路径而 ConPTY 没有
   路径，需要设计（`set new-console` 等变通），涉及 UX 决策；
3. 32 位 gdb 调不了 64 位程序——**建议主线改用 64 位工具链**
   （`D:\Linux\mingw64`，与用户现有配置一致）；
4. PDCurses-wincon 的 COLOR_PAIRS 上报值异常（COLOR_PAIRS=1 但 init_pair 实际
   工作、真彩输出正常）——接入时需实测 cgdb 的 highlight_groups 双路线
   （ANSI 表 / 惰性分配）在它上面的行为；
5. cp936 系统locale：cgdb 侧所有 UTF-8 文本输出在自写/接入层统一走宽字符或
   显式 UTF-8 处理，别依赖 `setlocale("")`（本机返回 CP936）；
6. GDB `new-ui` 若上游修复（值得提 bug），双通道原架构可复活，届时 MI 层
   可回切——设计时把「传输层」隔离成可替换件。

## 9. 建议的移植阶段

1. **阶段 0：地基**——lib/util 平台层（ConPTY 适配、io/terminal/主循环）、
   独立构建脚本、flex 生成物搬运；先把 `lib/util`、`lib/kui`、`lib/tgdb`
   （gdbwire + MI-over-pipes 传输）在 Windows 编译链接跑通，driver 程序
   全绿；
2. **阶段 1：无 UI 烟测**——tgdb_driver Windows 版：拉起 MI 模式 gdb、
   发 `-break-info` 收 `^done`、ConPTY 跑 inferior；
3. **阶段 2：UI 起屏**——swin 接 PDCurses-wincon，interface/sources 上屏，
   源码窗 + 状态条 + 合成 GDB 窗的静态显示；
4. **阶段 3：交互闭环**——kui 的 VK 输入层、行编辑器、命令翻译、断点/
   源文件/反汇编请求链路打通；
5. **阶段 4：inferior 终端 + 收尾**——ConPTY 窗口、resize、信号语义、
   `:set`/`:map` 全量回归、真彩主题。

---

## 附：实验脚本索引（都在 `.tmp/`，gitignored）

| 脚本 | 验证内容 |
|---|---|
| `test_newui_pipe.py` / `test_round2.py` / `test_round3.py` | new-ui 挂命名管道：反斜杠转义、banner、进程死亡 |
| `test_open_layers.py` | msvcrt open() 开命名管道、new-ui 开文件/CON |
| `test_conpty_arch.py` / `test_conpty_v3.py` / `test_final_arch.py` | ConPTY + MI 管道组合演进（含接线配方纠错过程） |
| `test_recipe_b.py` | STARTF_USESTDHANDLES+NULL 配方定版（cmd 控制组） |
| `test_isolate_death.py` | gdb 单独在 ConPTY 正常（$1=42）vs +new-ui 死亡的隔离对照 |
| `test_plan_b.py` | E1 文件通道死亡、E2 64 位 gdb 同死、E3 MI-over-pipes 全通 |
| `test_loop_behavior.py` | 控制台驱动事件循环行为 |
| `swtest.c` + `conpty_run.py` | curses 三后端能力矩阵 + VT 屏幕重建核对 |
