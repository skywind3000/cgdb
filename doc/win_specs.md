# CGDB Windows 移植实施规格与实测记录

本文档记录 Windows 移植的**实施过程**：每一步的决策、依据、实测结果与已知限制。
决策依据的可行性分析见 `doc/porting-feasibility.md`，代码架构参考见
`doc/reference.md`。分支：`windows`（自 master 切出，按里程碑提交）。

- [1. 目标与范围](#1-目标与范围)
- [2. 关键决策](#2-关键决策)
- [3. 目标架构](#3-目标架构)
- [4. M0 开工实验记录（全部通过）](#4-m0-开工实验记录全部通过)
- [5. 构建系统](#5-构建系统)
- [6. 平台层实现细节](#6-平台层实现细节)
- [7. tgdb 传输层改造](#7-tgdb-传输层改造)
- [8. 合成 GDB 窗口](#8-合成-gdb-窗口)
- [9. 输入链路](#9-输入链路)
- [10. 测试报告](#10-测试报告)
- [11. 已知限制与后续工作](#11-已知限制与后续工作)

---

## 1. 目标与范围

把 cgdb 移植到原生 Windows（mingw 工具链），最低系统 Windows 10 1809+：

- 可用 `mingw32-make -f win32/Makefile` 一条命令构建出 `cgdb.exe`（全静态，无 DLL 依赖）；
- 完整交互闭环：源码窗口、语法高亮、vim 风格键绑定、`:命令`、断点管理、
  运行/单步/继续、^C 中断、窗口缩放与 resize；
- GDB 会话走 `--interpreter=mi` 管道（new-ui 在原生 Windows GDB 上不可用，
  见 porting-feasibility.md §3.2），GDB 控制台窗口由 cgdb 合成；
- Linux 侧零改动（所有平台差异用 `#ifdef _WIN32` 守卫，回归基线 198 项）。

## 2. 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 工具链 | mingw32 (i686, gcc 15.2.0) | PDCurses-wincon 静态库只在 mingw32 有（mingw64 无 pdcurses）；GDB 用 `D:\Dev\mingw32\bin\gdb.exe`（用户指定） |
| curses 后端 | PDCurses 4.4 wincon，**静态链接**（`-lpdcurses_wincon -lwinmm`） | 真彩 24 位 SGR + UTF-8/CJK + 控制台 API 码页免疫，M0 全实测通过 |
| curses 头文件 | 根目录 `pdcurses.h` 包装（`PDC_WIDE` + `PDC_FORCE_UTF8` + 后缀符号映射） | 裸 `pdcurses/curses.h` 链接失败（`endwin_x64_4400` 后缀符号） |
| GDB 通道 | `gdb --nw -q -ex "set pagination off" -ex "set confirm off" --interpreter=mi` 匿名管道 | new-ui 死亡（上游 bug），MI-over-pipes 是 VS Code 同款成熟形态 |
| inferior 终端 | `-gdb-set inferior-tty \\.\pipe\cgdb_inf_<pid>` 命名管道 + 读线程 | M0 实测：MI 流干净、inferior 输出进管道、原始反斜杠路径可用 |
| 中断（^C） | gdb 与 cgdb 共享控制台 + `CREATE_NEW_PROCESS_GROUP` + `GenerateConsoleCtrlEvent(CTRL_BREAK, gdbpid)` | M0 实测：gdb 注入 DbgBreakPoint 线程，`*stopped` 到达，之后仍可应答 |
| 用户输入 | 控制台 `ENABLE_VIRTUAL_TERMINAL_INPUT`，kui 按字节流解析 ESC 序列 | kui 有 47 条硬编码 VT 绑定兜底（无 termcap 也可用）；ConPTY 注入的序列原样读回（实测） |
| resize | 主循环 100ms 轮询 `GetConsoleScreenBufferInfo` | 无需 SIGWINCH/管道；PDCurses `resize_term` 负责重建 |
| 行编辑器 | tgdb 内嵌字节级行编辑器（合成回显） | 见 §8 |
| flex | 生成物提交入库（`win32/generated/`），`win32/regen-lexers.sh` 在 WSL 重生成 | Windows 侧无 flex |
| include 策略 | 项目头一律 `-iquote`，禁用 `-I` 指向项目目录 | `lib/util/io.h` 会遮蔽 CRT `<io.h>`（flex 骨架经 `<unistd.h>` 引入 `<io.h>`，M0 实测踩雷） |

## 3. 目标架构

```
┌─ kui ─────────────────────────────────────────────┐
│ ReadFile(控制台, VT输入模式) → ESC序列字节流        │
│ (ESC[A/B/C/D、ESC[H/F、\x03...) → CGDB_KEY        │
└────────────┬──────────────────────────────────────┘
             │ user_input_loop (cgdb.cpp)
┌────────────▼──────────────────────────────────────┐
│ interface.cpp (不改显示逻辑)                        │
│  源码窗 sources / 状态条 / GDB窗 scroller_win      │
│  (scroller_win = 哑终端行模型, 替代 vterm)          │
└────────────┬──────────────────────────────────────┘
             │ scr_add / gdb_input keys
┌────────────▼──────────────────────────────────────┐
│ tgdb (命令队列+gdbwire 全保留, 仅换传输层)           │
│  行编辑器 → -interpreter-exec console "..."        │
│  ~记录/&记录/^error → console_output_callback      │
│  prompt → 合成 "(gdb) "                            │
└────────────┬──────────────────────────────────────┘
             │ CreatePipe (_open_osfhandle → fd)
┌────────────▼──────────────────────────────────────┐
│ gdb --nw -q --interpreter=mi                        │
│  (共享 cgdb 控制台 + 独立进程组 → CTRL_BREAK 可达)  │
│  inferior: -gdb-set inferior-tty \\.\pipe\...      │
└───────────────────────────────────────────────────┘

主循环 (cgdb.cpp win 版):
  WaitForMultipleObjects[控制台输入, MI读管道, 停机Event, inferior管道Event]
  + 100ms 超时轮询 (控制台尺寸变化→if_resize_term; gdb存活→quit)
```

## 4. M0 开工实验记录（全部通过）

实验脚本：`.tmp/run_m0_mi.py` + `.tmp/test_mi_child.py`（ConPTY 内运行，
gdb 与宿主共享控制台——与生产形态一致）、`.tmp/run_m0_ui.py` +
`vtio.c`/`colors.c`/`swtest_min.c`。GDB：`D:\Dev\mingw32\bin\gdb.exe` 17.1。

### E1: -interpreter-exec console 的输出形态 ✅

```
-send: -interpreter-exec console "print 1+2"
-gdb → ~"$1 = 3\n" ^done (gdb) 
```
- 用户命令的输出走 **`~` console-stream 记录**（不是 `^done,value=`），`^done` 结尾；
- **`(gdb) ` 提示符字面量出现在流里**（gdbwire 会将其作为 prompt 记录消费，
  显示层需要在 prompt 回调时合成回显）；
- 启动横幅：`=thread-group-added`、`=cmd-param-changed`（来自 -ex）。

### E8: MI 转义与错误形态 ✅

- MI 字符串转义：`\"`（引号）、`\\n`（literal backslash-n，供 console 命令用）；
- 错误时 **`&"Undefined command: ..."` 与 `^error,msg="..."` 成对出现，内容相同**；
  显示层只展示其一（`&` 记录在用户命令上下文显示，`^error` 用于内部请求时记日志）；
- `~` 记录内为纯 LF；inferior 管道输出为 CRLF（`\r\n`）——scroller 两者都要处理。

### E2a: 默认 inferior 输出污染 MI 管道 ✅（确认必须设 inferior-tty）

不设 inferior-tty 时，inferior 的 stdout 直接写进 gdb 的 stdout 管道，与 MI
协议裸字节混杂（实测 `INF: hello` 出现在 MI 流里）。

### E2b: inferior-tty 命名管道 ✅

- `-gdb-set inferior-tty \\.\pipe\cgdb_m0_inf`（**原始单反斜杠**，MI 未加引号参数
  原样传递）→ `^done`；
- `-exec-run` 时 gdb 以客户端身份连上管道（`ConnectNamedPipe` 返回）；
- inferior 输出 `SLEEP: started\r\n` 准确到达管道服务端，MI 流保持纯净；
- gdb 退出时管道 ReadFile 报 **err=109 (ERROR_BROKEN_PIPE)** —— 即 EOF 信号。

### E3: CTRL_BREAK 中断 ✅

gdb 以 `CREATE_NEW_PROCESS_GROUP` 创建、共享宿主控制台：
`GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, gdbpid)` →
gdb 新建调试线程注入 `DbgBreakPoint`：

```
~"Thread 3 received signal SIGTRAP, Trace/breakpoint trap.\n"
*stopped,reason="signal-received",signal-name="SIGTRAP",...,
  frame={...func="ntdll!DbgBreakPoint"...},thread-id="3"
```

之后 gdb 仍能正常应答命令并 `-gdb-exit`。注意：*stopped 落在注入线程
（thread 3, ntdll 帧）而非用户线程——源码窗口会先定位到该帧，后续可加
「SIGTRAP@DbgBreakPoint 后自动 thread 1」的润色（已知限制，见 §11）。

### E4: VT 输入模式 ✅

- `SetConsoleMode(hIn, ENABLE_VIRTUAL_TERMINAL_INPUT|ENABLE_EXTENDED_FLAGS)` 成功；
- **控制台输入句柄可入 WaitForMultipleObjects**（`WAIT_OBJECT_0`）；
- ConPTY 注入 `ab\x1b[A\x1b[H` → ReadFile 原样读回 `61 62 1b 5b 41 1b 5b 48`；
  `\x1b[21~\r`、`\x03`（^C 原始字节）、普通字符全部字节级往返成功；
- **initscr 会清掉 VT 输入模式**（0x280 → 0x88），endwin 恢复 →
  **必须在 `swin_start()` 之后重新设置控制台输入模式**（cgdb.cpp 启动时序）。

### E5: PDCurses-wincon 颜色能力 ✅

`start_color()` 之后：**COLORS=768、COLOR_PAIRS=589824**（此前 swtest 报
COLORS=0 是在 start_color 之前查询的假象）；`init_pair` 1..300 全部 OK；
`pair_content` 往返正确；输出为**真彩 SGR**（`38;2;255;135;0` 等）；CJK、
ACS 框线、bold/underline/reverse 全部正常。

两个重要衍生事实：
1. **PDC_WIDE 下 chtype 为 64 位**，`COLOR_PAIR(n)` 的颜色位在 bit 44+
   ——`sys_win.h` 的 `SWIN_CHTYPE`（`unsigned long`，win32=32 位）必须
   改为 64 位类型，否则 waddch/wattron 的颜色属性被截断；
2. 「画完立即退出的程序无输出」是 **ConPTY 拆机竞态**（conhost 渲染线程
   没来得及把帧刷进 VT 管道，`napms(300)` 后即正常）——探针程序要在退出前
   sleep；真实长驻 cgdb 不受影响，但 E2E 断言要在 `:q` 之前做。

### E6: flex 生成物 mingw 编译 ✅

9 个生成器（8 tokenizer + command_lexer）全部以
`g++ -std=c++11 -Wall` 零警告编译通过（WSL flex 2.6.4 生成，与 Linux
构建同源）。**踩雷记录**：`-I lib/util` 会让 mingw `<unistd.h>` →
`<io.h>` 解析到 cgdb 的 `lib/util/io.h`（含 `config.h` 报错）——
项目 include 一律用 `-iquote`。

### E7: PDCurses 在 ConPTY 内渲染 ✅

swtest 重编重跑：文本/框线/下划线/CJK/真彩 SGR 全部出现在 ConPTY 的
VT 流里（自写解析器对 `90-97` 亮色与 `38;2` 的 fg 记录有已知局限，
断言用原始字节核对）。

## 5. 构建系统

`win32/Makefile`（GNU make，mingw32-make 4.4.1，**必须从 Git Bash 运行**，因为
需要 sh 处理 `$(shell)` 和 `mkdir -p`）：

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"     # 仓库根目录下
mingw32-make -f win32/Makefile             # → .tmp/win-build/cgdb.exe
mingw32-make -f win32/Makefile drivers     # 单元 driver 程序
mingw32-make -f win32/Makefile check       # 自动化 driver 检查
mingw32-make -f win32/Makefile clean
```

关键设计：

| 项 | 内容 |
|---|---|
| 产物 | `.tmp/win-build/`（遵循 AGENTS.md 临时目录约定），obj 平铺 `.tmp/win-build/obj/` |
| 链接 | `-static -static-libgcc -static-libstdc++ -lpdcurses_wincon -lwinmm`，零 DLL 依赖 |
| include | **项目头一律 `-iquote`**（`lib/util/io.h` 会遮蔽 CRT `<io.h>`——mingw 的 `<unistd.h>`/flex 骨架会引入 `<io.h>`，`-I` 指项目目录必炸）；`-I$(MINGW)/include` 只给 pdcurses.h |
| config | 手写 `win32/config.h` + `win32/cgdb_custom_config.h`（后者提供 `ATTRIBUTE_PRINTF` 宏，作用同 autotools 构建里生成的同名文件） |
| 定义 | `-DHAVE_CONFIG_H -DWIN32_LEAN_AND_MEAN -D_WIN32_WINNT=0x0A00`（ConPTY/VT 输入常量） |
| 版本 | `-DPACKAGE_VERSION` 取自根目录 `VERSION` 文件 |
| flex | 生成物提交在 `win32/generated/`（9 个），`win32/regen-lexers.sh` 用 WSL flex 重生成 |
| 排除 | pseudo.cpp / fork_util.cpp / terminal.cpp（→ process_win.cpp + console_win.cpp）；lib/vterm、vterminal.cpp、scroller.cpp（→ scroller_win.cpp）；tester.cpp；readline 链接 |

M1 验证状态（2026-10-18）：lib/util（含 PDCurses swin 后端、process_win、
console_win）、lib/kui 全部 6 个对象、lib/tokenizer + 8 个 flex 生成物、
gdbwire.c 全部零警告编译；`tokenizer_driver` 以 c/rust/go/ada/d/asm 六种
语言喂真实源文件跑通；`cgdbutil_driver` 输出 ANSI 色卡。全静态链接。

## 6. 平台层实现细节

### 6.1 swin 层（sys_win.cpp，PDCurses 分支）

- include 分支新增 `HAVE_PDCURSES_H → <pdcurses.h>`（根目录包装，
  PDC_WIDE + PDC_FORCE_UTF8 + `endwin → endwin_u64_4400` 符号映射——该库
  只有 `endwin` 一个带后缀的符号，其余全部原名）。
- **属性编码**（见 sys_win.h 注释）：PDCurses 宽字符构建 chtype 64 位、
  颜色位在 bit 44+，塞不进 int。Windows 侧保持 int API：bit 0-7 存自定义
  SWIN_A_* 值（0x01=STANDOUT…0x20=BOLD），bit 20+ 存「颜色对+1」；
  `swin_wattron/wattroff` 在边界处用 `swin_expand_attr` 展开成真 chtype。
  全代码库只使用 SWIN_A_*（无裸 ncurses A_* 宏），UI 层零改动。
- `swin_color_pair()`：编码；ncurses 版照旧 `COLOR_PAIR()`。
- `swin_resizeterm` → `resize_term`（PDCurses 无 resizeterm 包装）。
- `swin_tigetstr` → NULL（无 terminfo，kui_term 落回硬编码绑定表）。
- `swin_set_escdelay` → no-op（ncurses 专有）。
- `swin_supports_default_color_pairs_extension` → false。
- `swin_start()` 末尾调用 `win_console_assert_input_mode()`：initscr 会
  清掉 VT 输入位（E4 实测 0x280→0x88，endwin 恢复）。
- `SWIN_CHTYPE` 在 win 下为 `unsigned long long`（ACS_* 等是 64 位）。
- COLOR_* 常量加 `#ifndef` 守卫（pdcurses.h 同名重定义警告）。

### 6.2 io 层（io.cpp）

- `io_data_ready(fd, ms)`：fd → HANDLE（fd 0 = 控制台输入句柄，其余
  `_get_osfhandle`），`WaitForSingleObject`（-1→INFINITE）。控制台输入
  句柄与管道读端都是合法等待对象（E4 实测）。
- `io_getchar(fd, ms, key)`：等待后 `ReadFile` 读 1 字节。**必须用
  ReadFile 而不是 CRT read()**——控制台在 VT 输入模式下 ReadFile 返回
  原始转义序列字节流，而 CRT read() 会过输入码页转换。
- `io_read/io_writen` 等不改：gdb 管道 fd 由 `_open_osfhandle` 生成，
  CRT read/write 原样可用；管道 EOF 由 read()==0 表达。

### 6.3 控制台（console_win.cpp/h，替代 terminal.cpp）

`win_console_setup/restore/assert_input_mode/size_changed`：
输入 `ENABLE_VIRTUAL_TERMINAL_INPUT|ENABLE_EXTENDED_FLAGS`（顺带关闭
quick-edit 选择模式），输出加 VT 处理位；原始模式只保存一次，
`assert_input_mode` 幂等重设；尺寸轮询比较 `srWindow` 与上次值。

### 6.4 进程（process_win.cpp/h，替代 pseudo/fork_util）

`win_spawn_debugger`：`CreatePipe`×2 + `STARTF_USESTDHANDLES`
（stderr 并入 stdout 管道）+ `CREATE_NEW_PROCESS_GROUP`（共享本控制台，
使 CTRL_BREAK 可达）；`_open_osfhandle` 把两端转成 CRT fd。
`win_interrupt_debugger`：`GenerateConsoleCtrlEvent(CTRL_BREAK, pid)`。
`win_inferior_tty_create`：`\\.\pipe\cgdb_inf_<pid>` 双工命名管道 +
读线程（ConnectNamedPipe 阻塞等待 gdb 在 inferior 启动时连入；ReadFile
循环把字节塞进临界区保护的缓冲并 SetEvent；管道断开 err=109 即 EOF）。

### 6.5 其他

- `fs_util.cpp`：`mkdir(path)`（无 mode 参数）+ PATH 分隔符 `;`。
- `sys_util.cpp`：`cgdb_is_debugger_attached → IsDebuggerPresent`。
- `kui_term.cpp`：`keyseq_initializer` 在 win 下直接 return（无
  termcap/tgetent/tgetstr），kui 使用 47 条硬编码 VT 绑定。
- `rline.cpp`：win stub（rline 只是 readline 按键序列 oracle，'/' 命令
  行编辑本就是 interface.cpp 手写；硬编码绑定已覆盖所需序列）。
- `kui` 其余、`gdbwire.c`、`tokenizer` + flex 生成物：零改动编译通过。

## 7. tgdb 传输层改造

（M2 填写）

## 8. 合成 GDB 窗口

（M3/M4 填写）

## 9. 输入链路

（M4 填写）

## 10. 测试报告

（M5 填写）

## 11. 已知限制与后续工作

（M6 填写）
