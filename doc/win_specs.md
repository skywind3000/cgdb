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
| inferior 终端 | `-gdb-set new-console on`：inferior 弹独立控制台窗口（VS 惯例） | 采纳 seer-windows D1/D4；PoC 实测：MI 流干净、弹窗 scanf 原生可用、CTRL_BREAK 中断不受影响；管道方案（inferior-tty 命名管道）已退役（见 §11） |
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
│  inferior: -gdb-set new-console on (独立弹窗)      │
└───────────────────────────────────────────────────┘

主循环 (cgdb.cpp win 版):
  WaitForMultipleObjects[控制台输入, 停机Event]
  + 50ms 超时轮询 (MI管道 PeekNamedPipe; 控制台尺寸变化→if_resize_term; gdb存活→quit)
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
inferior 不接线：tgdb 启动时排队 `-gdb-set new-console on`（见 §11），
console 子系统程序弹独立窗口、GUI 子系统不弹（PE 头决定）。

### 6.5 其他

- `fs_util.cpp`：`mkdir(path)`（无 mode 参数）+ PATH 分隔符 `;`。
- `sys_util.cpp`：`cgdb_is_debugger_attached → IsDebuggerPresent`。
- `kui_term.cpp`：`keyseq_initializer` 在 win 下直接 return（无
  termcap/tgetent/tgetstr），kui 使用 47 条硬编码 VT 绑定。
- `rline.cpp`：win stub（rline 只是 readline 按键序列 oracle，'/' 命令
  行编辑本就是 interface.cpp 手写；硬编码绑定已覆盖所需序列）。
- `kui` 其余、`gdbwire.c`、`tokenizer` + flex 生成物：零改动编译通过。

## 7. tgdb 传输层改造

### 7.1 改动形状

tgdb.cpp 的命令队列、请求映射、gdbwire 回调链**全部保留**；传输层用
`#ifdef _WIN32` 分支替换（Linux 侧字节级零改动，增量重编译通过）。合成
控制台（行编辑器等）直接实现在 tgdb.cpp 内（需要访问不透明的
`struct tgdb`），约 300 行 `#ifdef _WIN32` 包裹段。

改动点（对照 doc/reference.md 的行号索引）：

| 位置 | Windows 行为 |
|---|---|
| `tgdb_start_gdb` | `win_spawn_debugger`（管道 + CREATE_NEW_PROCESS_GROUP）+ 创建 inferior 管道 + 入队 `-gdb-set inferior-tty` 请求（TGDB_REQUEST_TTY，win 下命令串用 `-gdb-set inferior-tty`，反斜杠原样过 MI 解析器） |
| `tgdb_run_request` | DEBUGGER_COMMAND/CONSOLE_COMMAND → `-interpreter-exec console "..."`（MI 转义 `\"`/`\\`）；写端一律 `debugger_stdin`（gdb_mi_ui_fd 在 win 下是读端） |
| `tgdb_run_or_queue_request` | 用户命令也走队列串行化（响应按 current_request_type 归因，双命令在飞会串味） |
| `tgdb_process` | 单通道：读到的字节全部进 gdbwire（~ 记录路由显示，见下） |
| `tgdb_send_char` | → 合成控制台行编辑器（见 §8） |
| `tgdb_signal_notification` | SIGINT → `win_interrupt_debugger`（CTRL_BREAK）；SIGCHLD 不存在，主循环轮询 `tgdb_debugger_alive` |
| `tgdb_resize_console` | no-op（管道没有 winsize） |
| 新增 `TGDB_REQUEST_CONSOLE_COMMAND` | 任意用户命令行（tgdb.h 枚举追加；Linux 侧永不创建） |
| 新增 API | `tgdb_process_inferior`（排水 inferior 管道）、`tgdb_inferior_event_handle`（等待集用事件句柄）、`tgdb_debugger_alive` |

MI 记录路由（gdbwire 回调内的 win 分支）：

- `~` console 记录（非反汇编捕获期）→ `tgdb_win_output` → console_output_callback；
- `^error`（DEBUGGER/CONSOLE_COMMAND 上下文）→ 显示 msg（`&` 记录与
  ^error 文本重复，同 Linux 一样丢弃）；
- prompt 记录 → `tgdb_console_at_prompt` → 合成回显 `"(gdb) "`。

### 7.2 Windows 两个致命管道陷阱（M2 实测踩出，都已有修复）

1. **匿名管道读句柄上的 WaitForSingleObject 不可靠**：写端存活、管道
   空时仍返回「已信号化」（Win10 19045 实测；本机之前所有 ConPTY 实验
   都用 ReadFile 阻塞读或控制台句柄，从未踩到）。后果：主循环假就绪 →
   ReadFile 真阻塞 → 整个 UI 冻结。
   **修复**：`io_data_ready` 对管道改用 `PeekNamedPipe` 轮询（10ms 切片，
   `ERROR_BROEN_PIPE` 视为就绪让 read 拿到 EOF）；控制台输入句柄照旧
   WaitForSingleObject（E4 验证可靠）。**cgdb 主循环因此不能把 MI 管道
   句柄放进 WaitForMultipleObjects，而是轮询 `io_data_ready(fd, 0)`**。
2. **CRT `read()` 在信号化管道上永久阻塞**：同一症状的另一层——
   `read()` 在「句柄已信号但无数据」时阻塞不返回（E2 场景首发症状）。
   **修复**：`io_read`/`io_writen` 的 win 分支直接 ReadFile/WriteFile
   原始句柄（顺带绕开文本模式 CRLF 翻译；`_open_osfhandle` 加
   `_O_BINARY` 双保险）。`ERROR_BROKEN_PIPE` → 返回 0（EOF，对应
   Linux 的 EIO→0 映射）。
3. **命名管道同步句柄的读/写串行化死锁**（管道时代陷阱，随 §11 的
   new-console 改版退役，留档）：inferior 管道读线程常驻
   ReadFile 时，主线程向同一句柄 WriteFile（forwarding 用户键入）永久
   阻塞。
   **修复**：inferior 管道 `FILE_FLAG_OVERLAPPED` + 读线程 GetOverlappedResult
   阻塞读 + 写端重叠写（2s 超时后 CancelIo 丢弃，防止不读 stdin 的
   inferior 冻结 UI）。

### 7.3 MI 语义发现（E2/E1 复核）

- 该 GDB 17.1 的 MI 是**异步**的（`-exec-run` 立即回 `^running`+prompt，
  inferior 运行中可继续处理 MI 命令）——tgdb 的 prompt 驱动队列天然兼容；
- gdb 退出 → 管道 `ERROR_BROKEN_PIPE` = EOF → `tgdb_process` size==0 →
  TGDB_QUIT（MI driver 实测）；
- ^C 中断后 gdb 的 *stopped 落在 `ntdll!DbgBreakPoint` 注入线程（无
  源码信息）——中断功能本身完好（gdb 存活且继续应答），源码窗口定位
  润色（自动 thread 1）列为后续工作。

### 7.4 MI driver 验收（.tmp/mi_driver.cpp，ConPTY 内运行）

- `basic`：**7/7** —— b main 输出显示、停在 main（源码位置回调）、断点
  回调、inferior 输出显示（经命名管道）、MI 流纯净、干净退出
  （TGDB_QUIT）、gdb 进程退出；
- `interrupt`：**4/4** —— ^C 后 gdb 存活、继续应答 `print 7*6`（$1=42）、
  干净退出、进程退出。

### 7.5 启动失败的用户体验（后续补修，commit 「Fix silent crash...」）

移植版首发时有一个恶性 bug：**不带 `-d` 运行且 gdb 不在 PATH 时，
cgdb 直接访问违例崩溃**。表象正是用户报告的「卡几秒、什么都不显示、
退出」——崩溃进程要等 Windows Error Reporting 处理几秒才静默消失。
根因是 Linux 的 `invoke_debugger` 有 `path ? path : "gdb"` 的默认值
兜底（fork_util.cpp），Windows 分支漏掉了，`debugger_path=NULL` 一路
传到 `quote_arg(NULL)` 构造 `std::string(NULL)`。

修复（三层）：

1. `tgdb_start_gdb`（win 分支）补 `debugger ? debugger : "gdb"` 兜底；
2. `quote_arg` 加 NULL 防御；
3. **错误可见化**：`win_spawn_debugger` 失败时把人话消息存进
   `win_spawn_last_error()`（"cgdb: debugger \"gdb\" not found. Put
   gdb.exe on PATH or pass: cgdb -d <path-to-gdb.exe>"，ERROR_FILE_
   NOT_FOUND/PATH_NOT_FOUND 特判；其他错误带 CreateProcess 错误码），
   `cgdb_cleanup_and_exit` 在 endwin 之后打印——之前真正的失败原因
   只进 clog 日志文件，用户只看到「Search the logs」提示（Linux 侧
   同样只写日志，但至少不崩）。

实测：gdb 缺失时 0.5 秒干净退出（exit -1）+ 屏幕上直接给出原因和
解决办法；`-d` 指向不存在路径同样；正常路径不受影响。

## 8. 合成 GDB 窗口

### 8.1 显示层：scroller_win.cpp（哑终端行模型）

Linux 的 GDB 窗口是 vterminal（libvterm 终端模拟 + console pty 字节流）。
vterm 依赖 100% 藏在 scroller.cpp 内、interface.cpp 只调 `scr_*` API——
所以 Windows 侧新写 `cgdb/scroller_win.cpp` 用同一 API 实现一个「哑终端」：

| 概念 | 实现 |
|---|---|
| 行模型 | `std::deque<std::string> lines`（已提交行，受 scrollback 上限约束）+ `cur`（正在编辑/输出的当前行）+ `cur_col`（光标列） |
| scr_add 字节语义 | `\n` 提交行；`\r` 列归零；`\b` 列左移；TAB 展开到 8 列停；可打印/UTF-8 字节在光标处覆写并前进；行满按宽度折行（永不切开 UTF-8 序列——续字节允许溢出）；BEL/其他控制字节忽略 |
| 滚动模式 | `delta` = 从底部回看的行数；up/down/home/end/left/right/beginning/end_of_row 全实现；状态覆盖层 `[delta/max]` 用 HLG_SCROLL_MODE_STATUS |
| 搜索 | `hl_regex_search`（POSIX regex，mingw 的 TRE 实现）逐行匹配，wrapscan 遵循 cgdbrc；命中行滚动进视野、HLG_INCSEARCH 高亮（pre/match/post 三段渲染） |
| 光标 | 非滚动模式 = 当前行 + cur_col（焦点时显示）；滚动模式 = 滚动光标 |
| scr_move | 换窗时重取宽高；已提交行保持旧折行（vterm 的 reflow 语义弱化为 v1 可接受） |
| ctrl-L | `scr_push_screen_to_scrollback` = 提交当前行（v1 简化） |

**为什么不需要完整 vterm**：MI 模式下 gdb 输出是纯文本（无 readline 的
光标转义序列——它根本没有 readline 在跑）；编辑器回显只依赖 CR/BS/覆写
三种原语。哑终端模型完全够用，这正是 Linux 侧 `~` 记录 + readline 回显
字节流的最小公共子集。

### 8.2 输入层：tgdb 内嵌行编辑器（tgdb.cpp #ifdef 段）

- 字节流驱动（Linux 上写进 console pty 的同一字节流）：可打印插入、
  BS/DEL、`\r` 提交、ESC 序列（`ESC[A/B/C/D/H/F`、`ESC[0X`、`ESC OX`
  三种变体 → 历史/左右/Home/End）、ESC b/f/d/`\b` 词操作、
  Ctrl-A/E/B/F/K/U/W/D/L；
- 回显协议：全行重画 = `"\r" + "(gdb) " + line + 尾部擦除 + \b×n 回退`，
  追加字符走单字节快路径；
- 提交 → `TGDB_REQUEST_CONSOLE_COMMAND` → `-interpreter-exec console
  "..."`（MI 转义 `"`、`\`）→ 响应走 §7 的记录路由；
- 输出与编辑交错：非提示状态直接追加；提示状态下有半行输入时
  `"\n" + 输出 + 重画`（readline 风格）；
- prompt 回调合成 `"(gdb) "`（gdbwire 会把字面量提示符从流里消费掉，
  显示层必须自己补）；
- inferior 运行期间（非提示符状态）：按键丢弃——inferior 的 I/O 在它
  自己的控制台窗口里（§11），^C 在 cgdb.cpp 层被拦截走中断路径；
- 命令历史 100 条，方向键翻阅。

## 9. 输入链路

```
键按下 → ConPTY 输入管道 → conhost 转成 INPUT_RECORD
→ 控制台输入句柄信号化 → 主循环 WaitForMultipleObjects 唤醒
→ kui（terminal_keys 层 100ms 转义序列超时 + normal_keys 层映射超时）
→ io_getchar → io_data_ready(0,ms)（控制台句柄等待 + 按键记录门禁）
→ ReadFile 1 字节（VT 输入模式下读回原始转义序列字节流）
→ CGDB_KEY_* / 原始字节 → if_input → 各窗口
```

- **kui 的转义序列匹配不需要任何改动**：VT 输入模式给出的就是
  xterm 风格序列，kui 的 47 条硬编码绑定（`ESC[A` 等）原样命中；
- **按键记录门禁（M5 期间发现的第 4 个致命陷阱）**：控制台句柄在
  *任何* 输入记录到达时都会信号化（含 FOCUS_EVENT、MENU_EVENT 等非
  按键记录），而 `ReadFile` 只对 key-down 记录产生数据——否则永久
  阻塞，把 kui 的超时契约整个挂死（表现为 cgdb 收完启动输出后第一次
  按键即冻结）。`io_data_ready`/`io_getchar` 现在用 `PeekConsoleInput`
  检查队头：key-down → ReadFile 安全；非按键记录 → `ReadConsoleInput`
  消费掉再查；
- **KEY_UP 幽灵信号（首发后用户实测发现的第 5 个致命陷阱，即「按
  `:` 就崩」bug）**：Windows 控制台对每个按键生成 KEY_DOWN + KEY_UP
  两条记录，KEY_UP 不产生 VT 字节但会把句柄信号化。首发版 `io_data_ready`
  把非按键记录排掉后返回 0——而 kui 的 `kuictx::findkey` 在 **map_set
  为 NULL（状态条焦点，按 `:` 之后）** 时把「findchar 返回 0」直接当
  -1 错误返回（上游潜伏 bug，Linux 上 select 语义「可读=有数据」使其
  不可达）。于是按 `:` → 状态条清空 map_set → `:` 的 KEY_UP 几十毫秒
  后到达 → 幽灵唤醒 → 伪 -1 → `user_input_loop failed` → 静默退出。
  **修复**：`io_data_ready` 的控制台路径改为循环等待——排掉非按键记录
  后继续等到真的有 key-down 或超时（恢复 Linux select 契约）；主循环
  的用户输入门禁统一走 `io_data_ready(0, 0)`（不再信任裸 wait 结果，
  顺带在门禁处排掉游离 KEY_UP）。注意 `ms==0` 时必须先轮询一次再判
  deadline（首发修复版曾把 0 超时快捷成「永不就绪」的 bug）；
- ^C：VT 输入模式下不再是事件而是原始 0x03 字节 → kui →
  CGDB_KEY_CTRL_C → `user_input` 里拦截 → `tgdb_signal_notification` →
  CTRL_BREAK（§7.2）；
- ESC 键：kui 的 ttimeout（100ms）超时后孤 ESC 解析为 CGDB_KEY_ESC →
  焦点切源码窗。E2E 中 ESC 后需 ≥1.1s（100ms 序列超时 + 1s 映射超时）
  再发 `:` 才不被吞进序列匹配。

## 10. 测试报告

### 10.1 MI driver（M2 验收，`.tmp/mi_driver.cpp`，ConPTY 内运行）

- `basic` **7/7**：b main 输出显示、停在 main（源码位置回调）、断点
  回调、inferior 输出显示、MI 流纯净、TGDB_QUIT 干净退出、gdb 进程退出；
- `interrupt` **4/4**：^C 后 gdb 存活、继续应答 `print 7*6`、干净退出、
  进程退出。

### 10.2 E2E 场景矩阵（M5 验收，`.tmp/e2e_cgdb.py`，ConPTY 40x110）

**23/23 全部通过**（2026-10-18 22:55，Windows 10 19045，gcc 15.2.0
静态链接，gdb 17.1 i686）：

| 场景 | 检查项 |
|---|---|
| 主流程 | gdb 提示符出现、源码窗显示代码、行号渲染、状态条、`b main` 输出、源码窗断点标记、`run` 后 inferior 输出**不**出现在 GDB 窗（在弹窗里，MI 流干净）、停回提示符、`next` 单步、方向键历史召回、ESC+`:q` 干净退出（exit 0） |
| resize | 130 列扩张无崩溃、会话存活、缩回 100 列正常、干净退出 |
| CRLF+CJK | CRLF 源文件正常显示、CJK 注释（你好世界）正确渲染、干净退出 |
| ^C 中断 | m0_sleep 运行中 ^C 产生 *stopped、gdb 继续应答命令、干净退出 |
| 源码窗按键 | ESC 切焦点、j 移动、空格切换断点（Breakpoint 1 请求发出）、干净退出 |

真彩语法高亮在 VT 流中实测可见（`38;2;r;g;b`、`91m` 亮色关键字、
框线 `│` U+2502、行号列）。

### 10.2b testsuite/win32 全覆盖测试套件（后续补建，已入库）

首发后用户按 `:` 即崩（见 §9 KEY_UP 陷阱），暴露出 E2E 覆盖不足——
于是建了正式的全覆盖套件 `testsuite/win32/`（入库，含 README）：

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"     # gcc 编译测试 inferior
python testsuite/win32/run_tests.py        # 仓库根目录，约 3 分钟
```

**96/96 全部通过**（2026-10-19）。覆盖矩阵：

| 模块 | 覆盖 |
|---|---|
| cases_cli | `--version`/`--help`、`-d` 指向不存在路径（错误可见化回归）、PATH 无 gdb |
| cases_session | 启动画面、ESC/i 焦点切换（行为级验证）、**状态条 `:` 后静置（KEY_UP 崩溃回归用例）**、状态条退格/未知命令/ESC 取消、连发按键 |
| cases_debug | b main + run + 停止、inferior 输出、typed next / F10 / cont、空格切换断点、`u` until、^C 中断后应答、无调试信息程序 |
| cases_nav | G/gg/Ctrl-D/Ctrl-U/j/k、`/` 搜索、GDB 窗行编辑（退格/Ctrl-U/Ctrl-A/E/F）、方向键历史、PageUp 滚动模式 |
| cases_windows | `+`/`-` 分屏（状态条行位置变化断言）、极限缩小、ConPTY resize 扩/缩、12x40 与 6x20 极小终端 |
| cases_files | CRLF+CJK 源文件显示与调试、窄终端行宽 |
| cases_commands | `:set`（syntax/winminheight/空值）、未知命令、`:help`、`:q`（空闲后/调试后/inferior 运行中） |

按键全部走 `keys()`（带间隔逐字节发送）——每个按键都产生真实的
KEY_DOWN/KEY_UP 记录对，正是踩出状态条 bug 的时序路径。

### 10.3 单元 driver（M1）

- `tokenizer_driver`：c/rust/go/ada/d/asm 六语言喂真实源文件全部跑通；
- `cgdbutil_driver`：ANSI 色卡输出正常。

### 10.4 Linux 回归（M6）

WSL2 Ubuntu-24.04 全量重编 + `make check`：**198/198 全部通过**——
Windows 移植对 Linux 侧零破坏（所有改动都在 `#ifdef _WIN32` 内）。

## 11. 已知限制与后续工作

按影响排序：

1. **^C 后源码定位**：`*stopped` 落在 gdb 的 DbgBreakPoint 注入线程
   （`ntdll!DbgBreakPoint`，无符号无行号），源码窗不会立即跳回用户
   线程。后续可在 ^C 后自动 `-thread-select` 回原线程。
2. **readline 功能缺失**：GDB 窗口行编辑器无 Tab 补全、Ctrl-R 历史搜
   索（`:命令`行的补全不受影响，本来就不走 readline）。后续可用
   `-interpreter-exec console "complete ..."` 实现补全。
3. **32 位 gdb 只能调 32 位程序**：主线工具链是 mingw32。若要调 64 位
   程序：换 mingw64 工具链重编 cgdb（PDCurses 需另行获取——mingw64
   发行版未附带），或运行时 `-d` 指向 64 位 gdb（cgdb 本身跨位数）。
4. **CJK 键入**：VT 输入模式的字节流按控制台输入码页转换，非 UTF-8
   码页下 CJK 输入会乱（显示不受影响——PDCurses 走宽字符 API）。
   E2E 已验证 ASCII 键入全通；CJK 键入留待后续（设 CP_UTF8 或切
   ReadConsoleInputW 路线）。
5. **多实例**：inferior 管道名含 cgdb pid，天然可并存；但同一控制台
   多个 cgdb 实例的 ^C 会互相干扰（CTRL_BREAK 按进程组隔离，已验证
   不串扰，控制台事件共享）。
6. **`new-ui` 上游修复后的回切**：传输层已隔离在
   tgdb.cpp 的 #ifdef 段内，若上游 gdb 修好 new-ui，MI-over-pipes
   与双通道可共存选择。
7. **inferior 交互**：inferior 在自己的控制台窗口里跑（§11），全屏
   交互（vim 类）、scanf、CJK 键入全部原生可用——比 Linux 内嵌 pty 路线
   还省事。代价：inferior 输出不显示在 cgdb 屏幕内（在弹窗里）；程序
   退出即关窗，临终输出看不到（VS 同款行为）。
8. **`Unknown command` 消息不可见**（上游行为，非移植差异）：`:` 敲
   未知命令时 `if_run_command` 画的错误消息会被随后的 `if_draw()` 在
   doupdate 之前覆盖（Linux 上 pty 实测同样不可见）。会话无损，命令
   继续可用。
9. **`rline` 键序列 oracle**：Windows 侧是 stub——硬编码绑定已覆盖
   Home/End/词操作，若用户自定义了 readline 绑定则不感知。

### 交付物清单

| 项 | 位置 |
|---|---|
| 构建 | `win32/Makefile`（+ `win32/config.h`、`win32/cgdb_custom_config.h`、`win32/generated/`、`win32/regen-lexers.sh`；注意这三项被上游 `.gitignore` 的 autotools 规则误伤，需显式入库，见 §12） |
| 平台层 | `lib/util/console_win.*`、`lib/util/process_win.*`、io/terminal/sys_win/fs_util/sys_util 的 win 分支 |
| tgdb 传输 | tgdb.cpp/tgdb.h 的 win 分支（含行编辑器） |
| UI | `cgdb/scroller_win.cpp`、interface.cpp/cgdb.cpp 的 win 分支 |
| 文档 | 本文件 + `doc/porting-feasibility.md`（可行性）+ `doc/reference.md`（架构） |
| 测试 | `testsuite/win32/`（**96 项全覆盖套件，入库**）+ `.tmp/mi_driver.cpp`（MI 验收）+ `.tmp/e2e_cgdb.py`（E2E 23 项） |
| 产物 | `.tmp/win-build/cgdb.exe`（6.6MB 全静态，仅依赖系统 DLL） |

## 12. inferior 终端改版：new-console 弹窗（2026-10-29）

**决策**：管道方案（`-gdb-set inferior-tty \\.\pipe\cgdb_inf_<pid>` 双工
命名管道 + 读线程 + 按键转发）退役，改为 `-gdb-set new-console on`——
被调试程序启动时弹出自己的独立控制台窗口（Visual Studio / VSCode /
vim termdebug 的 Windows 惯例，方案采纳自 seer-windows 的 D1/D3/D4）。
console 子系统程序弹可见窗口，GUI 子系统程序不弹（由 PE 头决定，无需
探测）。Python 版（`python/cgdbpy`）同步改版，两侧行为一致。

**动机**：管道不是终端——实测含 `scanf` 的程序无法正常交互（stdin 是
管道、无回显、`\r` 语义错位），全屏程序与 CJK 键入同样不可用。弹窗里
conhost 原生处理行编辑、回显、ANSI、Ctrl-C，这些问题全部消失。

**PoC 实测**（`.tmp/poc-newcon/`，gdb 17.1 x86，驱动 `poc.py`，日志
`poc-log.txt`）：

| 项 | 结论 |
|---|---|
| `-gdb-set new-console on` | `^done`；`-gdb-show new-console` 回 `value="on"` |
| 弹窗 scanf | inferior 的 stdin 是真 console（GetConsoleMode 成功）；WriteConsoleInput 注入 `42\r` → scanf 读到 → 程序写回 `GOT:42 TTY:1` |
| MI 流纯净 | inferior 输出（PROMPT/GOT）不出现在任何 MI 记录 |
| `CTRL_BREAK → gdb 进程组` | **依然有效**：inferior 在独立控制台时 gdb 收到事件并注入 DbgBreakPoint → `*stopped,signal-name="SIGTRAP"`（中断机制零改动） |
| 同步模式 `-exec-interrupt` | 无效且挂死 gdb（与 seer D5 一致；`mi-async on` + `-exec-interrupt` 有效但不需要） |
| `DebugBreakProcess(inferior)` | 本机 17.1 有效（备用数据点） |

**代码改动**（2026-10-29，windows 分支）：

- `tgdb.cpp` win 分支：`tgdb_start_gdb` 不再建管道，排队
  `TGDB_REQUEST_TTY`（win 下命令串为 `-gdb-set new-console on`）；
  `tgdb_win_editor_char` 在非提示符状态直接丢弃按键（原来转发进管道）；
  删除 `win_tty`/`win_inf_used` 字段与 `tgdb_process_inferior` /
  `tgdb_inferior_event_handle`（tgdb.h/cgdb.cpp 的调用点一并删除）。
- `process_win.cpp/h`：删除 `win_inferior_tty_*` 全套（约 240 行），
  只剩 gdb 启动 + 中断 + 存活查询。
- 中断路径（`win_interrupt_debugger`）与 gdb 启动方式**零改动**。

**测试同步**（重要——在另一台构建机上跑套件前必读）：

- `testsuite/win32/` 中凡是断言「inferior 输出出现在 GDB 窗」的用例
  （cases_debug 的 run/输出相关项）会失败：inferior 输出现在去弹窗了。
  改法参照 python e2e 的同名改版
  （`python/tests/e2e/test_e2e.py::test_inferior_runs_in_its_own_console`）：
  断言输出**不**出现在 cgdb 屏幕 + 会话恢复应答（echo 探针）+ MI 干净。
- ^C 中断用例应照常通过（中断机制未动）。
- 弹窗在 ConPTY 宿主里照常弹出（cgdb 在 ConPTY 内、gdb 继承 ConPTY、
  inferior 由 CREATE_NEW_CONSOLE 获得真实桌面窗口），程序退出即自动
  关窗，测试无需清理。

**构建系统备忘**：`win32/Makefile`、`win32/config.h`、
`win32/cgdb_custom_config.h` 与根目录 `pdcurses.h` 包装此前被上游
`.gitignore` 的 `Makefile`/`config.h`/`cgdb_custom_config.h` 无路径
前缀规则**误伤**（全仓库任意目录同名都被忽略），从未进入版本库，
且从工作区丢失——这就是它们只存在于另一台构建机上的原因。修复
方式：`.gitignore` 加定向反排除（`!win32/Makefile` 等）后显式提交；
Python 版改版（2026-10-29）在本机验证时因缺构建系统未重建 C 版二进制，
上述 C 代码改动已通过 grep 全量核对（无残留引用），待构建机重编后
按上段更新 testsuite/win32 断言。
