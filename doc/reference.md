# CGDB 源码参考手册（Windows 移植底稿）

本文档面向移植工作，系统梳理 cgdb 的项目结构、代码原理与依赖。所有行号以当前
master（`ae240a2`）为准。工具链现状、构建命令、Windows 侧已探测结论见仓库根的
`AGENTS.md`，本文不重复，只讲代码本身。

目录：

- [1. 总体架构](#1-总体架构)
- [2. 生命周期时序](#2-生命周期时序)
- [3. 主程序 cgdb/cgdb.cpp](#3-主程序-cgdbcgdbcpp)
- [4. UI 层（cgdb/ 目录）](#4-ui-层cgdb-目录)
- [5. lib/tgdb —— GDB 通信抽象层](#5-libtgdb--gdb-通信抽象层)
- [6. lib/kui —— 按键映射引擎](#6-libkui--按键映射引擎)
- [7. lib/util —— 平台基础设施](#7-libutil--平台基础设施)
- [8. lib/rline —— readline 键表导出](#8-librline--readline-键表导出)
- [9. lib/tokenizer —— 语法高亮词法](#9-libtokenizer--语法高亮词法)
- [10. lib/vterm —— 内嵌终端模拟器](#10-libvterm--内嵌终端模拟器)
- [11. 构建系统](#11-构建系统)
- [12. 外部依赖清单](#12-外部依赖清单)
- [13. POSIX 依赖矩阵（移植风险总表）](#13-posix-依赖矩阵移植风险总表)
- [14. 已知陷阱与移植注意点](#14-已知陷阱与移植注意点)

---

## 1. 总体架构

### 1.1 一句话架构

cgdb 是一个双进程、单线程、事件驱动的 curses 程序：主进程 fork 出一个跑在 pty 上的
gdb，用 `select()` 同时等待「用户按键 / gdb 控制台输出 / gdb MI 输出 / resize /
信号」五路输入，上半屏画源码、下半屏用一个内嵌终端模拟器（libvterm）复现 gdb 的
控制台会话。

### 1.2 双进程模型与 GDB 启动方式

GDB 不是被当库用的，而是被当黑盒子子进程用的。`lib/util/fork_util.cpp:181-275`
的 `invoke_debugger()` 拼出这样的命令行（`fork_util.cpp:223-227`）：

```
gdb --nw -ex "new-ui mi /dev/pts/N"  [用户在 -- 之后传的参数]
```

然后用 `pty_fork()`（`lib/util/pseudo.cpp:663`）把 gdb 拉起来。一共涉及**两对
pty**：

| pty | 创建位置 | master 归谁 | gdb 侧接什么 | 用途 |
|---|---|---|---|---|
| console pty | `pty_fork()`（fork_util.cpp:248） | cgdb（即 `tgdb->debugger_stdin/stdout`） | gdb 的 stdin/stdout | 用户和 gdb 的交互控制台，gdb 内部的 readline 在这里做行编辑 |
| MI pty | `tgdb_initialize()` 里 `pty_pair_create()`（tgdb.cpp:725） | cgdb（即 `tgdb->gdb_mi_ui_fd`） | `new-ui mi` 开出的 MI 解释器 | cgdb 主动发 MI 命令（-break-info 等）并收结构化回复 |

`--nw` 表示 gdb 不用自己的 curses UI；`new-ui mi <slavename>` 是 GDB 7.12+ 的
命令，把 MI 解释器挂到第二个 pty 上（tgdb.cpp:1059-1082 还会在控制台输出里找
`Undefined command: "new-ui".` 来探测老版本 gdb 并退出）。

MI pty 创建后立刻 `tty_set_echo(masterfd, 0)` 关回显（tgdb.cpp:732）。
console pty 的初始窗口尺寸在 `pty_fork` 的 winsize 参数里传入（fork_util.cpp:198-208），
之后 resize 时用 `ioctl(TIOCSWINSZ)` 同步（tgdb.cpp:1167-1187）。

`pty_fork` 特意不用 `forkpty()`，以保证 master fd 留在父进程（pseudo.cpp:671 附近
注释）。子进程 exec 失败时 `exit(0)`（fork_util.cpp:260-265）。

### 1.3 模块分层图

```
                 ┌──────────────────────────────────────────┐
                 │  cgdb/  主程序 + UI                       │
                 │  cgdb.cpp(主循环) interface sources       │
                 │  scroller vterminal highlight_groups      │
                 │  cgdbrc filedlg logo command_lexer.lpp    │
                 └──────┬───────────┬───────────┬───────────┘
                        │           │           │
                 ┌──────▼──┐  ┌─────▼────┐ ┌───▼─────────┐
                 │ lib/tgdb│  │ lib/kui  │ │ lib/rline   │
                 │ 命令队列│  │ 按键映射 │ │ readline键表│
                 │ gdbwire │  └─────┬────┘ └─────────────┘
                 └──────┬──┘        │
                        │      ┌────▼─────────┐
                 ┌──────▼──────▼─────┐        │
                 │  lib/util         │◄───────┤
                 │ io/pseudo/terminal│  lib/tokenizer (高亮词法)
                 │ sys_win(curses)   │  lib/vterm    (终端模拟, 被vterminal.cpp用)
                 └───────────────────┘
```

- `cgdb/` 只通过 `lib/util/sys_win.h` 的 `swin_*` 抽象层碰 curses，UI 代码零直接
  curses include（详见 4.7）。
- `lib/tokenizer` 和 `lib/vterm` 是两个叶子库，只被 UI 消费。
- 所有模块互相依赖的公共底座是 `lib/util`（`cgdb_LDADD` 里 util 排最后，
  cgdb/Makefile.am:22-28）。

### 1.4 四条核心数据流

**A. 用户按键（输入方向）**

```
stdin 字节流
 → lib/util/io.cpp  io_getchar/io_data_ready (select+非阻塞read)
 → kui_manager 两层 kuictx：
     terminal_keys 层: 转义序列(ESC[A 等)折叠成 CGDB_KEY_* 单值, 超时=keycode_timeout
     normal_keys  层: :map/:imap 的 vim 风格替换,      超时=mapping_timeout
 → cgdb.cpp user_input() (cgdb.cpp:423-466)
 → if_input(key) 按焦点分发 (interface.cpp:1594 → internal_if_input:1516)
     ├─ CGDB 焦点: 源码窗操作(滚动/断点/mark)
     └─ GDB 焦点: gdb_input(), 未消费的键返回 1
 → 返回 1 的键经 kui_term_get_ascii_char_sequence_from_key 还原成原始字节序列
   (cgdb.cpp:449-461, 抽象键只在 cgdb 内部存活, 出口必须反翻译)
 → tgdb_send_char → io_write_byte 写 console pty master → gdb 内的 readline
```

**B. GDB 控制台输出（console pty，master 可读）**

```
select 报告 gdb_console_fd 可读 (cgdb.cpp:878)
 → tgdb_process() (tgdb.cpp:1084-1131): 读 4096 字节
     ├─ 顺带做 new-ui 支持探测
     └─ console_output_callback (cgdb.cpp:369)
 → if_print (interface.cpp:1603-1628)
 → scr_add = vterminal_write (scroller.cpp:291-294)
 → vterm_input_write: libvterm 把 ANSI 转义序列解析成屏幕单元格
 → scr_refresh 时逐 cell 取出画进 curses 窗口 (scroller.cpp:568-664)
```

也就是说：**gdb 输出里的所有转义序列（清行、移光标、readline 的行重绘）都由
libvterm 吃掉，cgdb 自己不做任何转义解析**。

**C. MI 输出（MI pty，master 可读）**

```
select 报告 gdb_mi_fd 可读 (cgdb.cpp:871, 注意先于 console 处理, 见 3.1)
 → tgdb_process() → tgdb_commands_process = gdbwire_push_data (tgdb.cpp:586-589)
 → gdbwire 状态机切出 MI 记录, 回调 tgdb 注册的 wire_callbacks (tgdb.cpp:566-574):
     stream_record: console/log/target 流 (target 流会 \n→\r\n 后转 console_output)
     async_record:  *stopped / 断点增删改 → 触发 update_file_pos / 重新要断点
     result_record: 按当前 request 类型分发解析 (tgdb.cpp:506-545)
     prompt:        gdb 回到提示符 → tgdb_console_at_prompt → 出队下一条命令
 → 解析结果经 tgdb_callbacks 推给 cgdb.cpp (cgdb.cpp:388-397)
 → update_file_position / update_breakpoints / update_source_files /
   update_disassemble (cgdb.cpp:498-668) → sources.cpp / interface.cpp 更新显示
```

**D. resize 与信号**

```
SIGWINCH → signal_handler (interface.cpp:751-765) write 到 resize_pipe[1]
        → 主循环 select 到 resize_pipe[0] → cgdb_resize_term (cgdb.cpp:731-759)
        → 若管道里还有存货则跳过本帧(去抖) → if_resize_term (interface.cpp:631-644)
        → ioctl(TIOCGWINSZ) + swin_resizeterm + if_layout

SIGINT/SIGTERM/SIGQUIT/SIGCHLD → 同一 handler write 到 signal_pipe[1]
        → 主循环 → cgdb_handle_signal_in_main_loop (cgdb.cpp:774-786)
        → tgdb_signal_notification (tgdb.cpp:1411-1434):
            SIGINT:  取 console pty 的 VINTR 字符直接写给 gdb (相当于给 gdb 发 ^C)
            SIGQUIT: 同上用 VQUIT
            SIGCHLD: waitpid 收尸 + 发 TGDB_QUIT
```

---

## 2. 生命周期时序

### 2.1 启动序列（cgdb.cpp:1087-1189 `main`）

顺序非常讲究，改动前先理解依赖关系：

| 步骤 | 行号 | 做什么 | 为什么在这个位置 |
|---|---|---|---|
| 1 | 1089 | `parse_long_options`（-d/-w/-v/-h，-- 后面的给 gdb） | |
| 2 | 1092 | `init_home_dir`：`$CGDB_DIR` 或 `~/.cgdb`，建 `logs/` 子目录 | 日志要在一切之前开 |
| 3 | 1110 | `cgdb_start_logging`：clog 三个通道（cgdb 主日志 / gdbio / gdbmiio） | |
| 4 | 1113 | `cgdbrc_init`：配置默认值表 | 早于一切消费者 |
| 5 | 1115 | `tgdb_initialize`：建 MI pty、建 gdbwire、预排 3 个初始请求（当前帧位置、断点表、disassemble /s 探测） | 见 5.1 |
| 6 | 1123 | `rline_initialize`：临时 pty 上跑 `rl_initialize`，导出键序列 | 必须在 tty_cbreak/kui 之前（见 8） |
| 7 | 1128 | `tty_cbreak(STDIN)`：termios 关 ECHO/ICANON | kui 需要裸字节流 |
| 8 | 1133 | `init_kui`：`kui_manager::create` + kui_map/kui_imap + 把 readline 键序列注册进 kui + cgdbrc_attach 超时联动 | |
| 9 | 1139 | `swin_start`：curses initscr + 颜色探测 | |
| 10 | 1144 | `hl_groups_initialize`：依赖 curses 颜色能力探测结果 | |
| 11 | 1156 | `parse_cgdbrc_file`：读 `~/.cgdb/cgdbrc` | 刻意放在 if_init 之前——`:set` 可能关颜色；此时窗口未建，相关回调有 NULL 守卫（interface.cpp:1785） |
| 12 | 1159 | `if_init`：注册信号、ioctl 取初始尺寸、建 filedlg | |
| 13 | 1165 | `if_layout`：首次创建三个窗口（源码/状态条/gdb）并首绘 | |
| 14 | 1168 | `start_gdb` → `tgdb_start_gdb` → `invoke_debugger` | 用 13 算出的 gdb 窗尺寸初始化 pty winsize |
| 15 | 1173-1182 | `init_resize_pipe` / `init_signal_pipe` | 注意：在 if_init 注册信号 handler **之后**才建管道，见 14 的陷阱清单 |
| 16 | 1185 | `main_loop` | |

### 2.2 关闭序列（cgdb.cpp:904-955 `cgdb_cleanup_and_exit`）

与启动严格反序（cgdb.cpp:911-914 注释）：

```
swin_scrl/move 留提示行 → swin_endwin(恢复终端) → if_shutdown(删窗口:
status_win → scr_free(vterm+regex+窗) → source_free → vseparator) →
hl_groups_shutdown → tgdb_shutdown(关 debugger_stdin, 销 gdbwire) →
tty_set_attributes(恢复 termios) → tgdb_close_logfiles → exit
```

`new_ui_unsupported` 时额外打 "cgdb requires gdb 7.12 or later"。
clog 有 ERROR 记录时提示用户看日志目录。

---

## 3. 主程序 cgdb/cgdb.cpp

### 3.1 主循环（cgdb.cpp:788-896）

单线程 `select()`，五个 fd（cgdb.cpp:800-811）：

| fd | 含义 |
|---|---|
| `STDIN_FILENO` | 用户按键 |
| `gdb_console_fd` | console pty master（读 = gdb 控制台输出） |
| `gdb_mi_fd` | MI pty master |
| `resize_pipe[0]` | SIGWINCH 自管道 |
| `signal_pipe[0]` | 其他信号自管道 |

select 返回后的检查顺序（有讲究）：

1. `signal_pipe` —— 信号最先处理
2. `resize_pipe` —— 去抖逻辑：管道里还有数据说明 resize 风暴未停，跳过本帧（cgdb.cpp:740-751）
3. `STDIN` → `user_input_loop()`：内层 `do{user_input()}while(kui_ctx->cangetkey())`
   把 kui 缓冲一次抽干；受 `kui_input_acceptable` 门控（cgdb.cpp:118-139 的长注释：
   打开文件对话框等待 gdb 回文件列表期间不许继续吃键）
4. **`gdb_mi_fd` 先于 `gdb_console_fd`**（cgdb.cpp:848-870 长注释）：issue #352，
   `monitor help` 时 prompt 在 console、target 输出在 MI，先处理 MI 才能保证
   target 输出显示在 prompt 之前
5. `gdb_console_fd` → `gdb_input()` → `tgdb_process`；处理完若 kui 还有存货再补
   一轮 `user_input_loop()`（cgdb.cpp:883-891，filedlg 场景）

EINTR 直接 continue（cgdb.cpp:815-816）。cygwin 特例：select 假唤醒导致 kui
非阻塞读返回 EAGAIN 时 `user_input_loop` 返回 -1，靠 `errno == EAGAIN` 容错
continue（cgdb.cpp:837-846 的注释；Windows 控制台上同类竞态大概率重演，移植时
建议保留同款防御）。

### 3.2 关键全局量（cgdb.cpp:99-170）

- `struct tgdb *tgdb` —— tgdb 实例，interface.cpp 直接 extern 引用
- `kui_ctx`（`std::unique_ptr<kui_manager>`）、`kui_map`/`kui_imap`（两个
  `kui_map_set`，分别对应 `:map`/`:imap`）
- `gdb_console_fd` / `gdb_mi_fd` —— select 监视的两个 pty master
- `kui_input_acceptable` —— 输入门控（见 3.1）
- `resize_pipe` / `signal_pipe` —— 信号自管道（非 static，interface.cpp 的
  handler extern 引用）
- `term_attributes` —— tty_cbreak 保存的原始 termios
- `cgdb_home_dir` / `cgdb_log_dir` —— `~/.cgdb` 和 `~/.cgdb/logs`

### 3.3 `run_shell_command`（cgdb.cpp:185-219）

`:shell` / `:!` 的实现：endwin → 恢复 termios → `system()` → `fgetc` 等回车 →
重新 cbreak → if_draw。Windows 上对应「挂起 curses → 恢复控制台模式 → 跑子进程」
的完整往返。

---

## 4. UI 层（cgdb/ 目录）

`cgdb/tester.cpp` 不参与构建（历史遗留），别看它。

### 4.1 interface.cpp —— 布局、焦点、tgdb 交互枢纽（1838 行）

**单例式设计**：整个 UI 状态是文件级静态变量（interface.cpp:107-134），没有上下文
结构体。核心：`gdb_scroller`（下半屏）、`src_viewer`（上半屏 `sviewer*`）、
`status_win`（1 行状态条）、`vseparator_win`（竖布局才有）、`focus`（四态：
GDB/CGDB/CGDB_STATUS_BAR/FILE_DLG，interface.h:142）、`screen_size`（struct winsize）。

**布局几何**：窗口坐标每帧由 `get_src_*/get_gdb_*/get_sep_*` 函数族现算
（interface.cpp:217-372），不缓存。水平布局（默认）：源码窗 `(行数+0.5)/2 +
window_shift`；竖布局：左右分屏中间 1 列分隔窗。`window_shift` 被
`validate_window_sizes()` 夹在 winminheight/winminwidth 允许的范围内
（interface.cpp:560-586）。

**窗口重建哲学**：`create_swindow()`（interface.cpp:377-401）不做 wresize，而是
位置或尺寸任一变化就 `swin_delwin` 旧窗 + `swin_newwin` 新窗；`nlines<=0` 时窗口
保持 NULL。源码窗换窗走 `source_move()`，gdb 窗走 `scr_move()`。

**`if_layout()`**（interface.cpp:588-624）是 resize 的总入口，顺序：
算几何 → 重建源码窗 → **`tgdb_resize_console(tgdb, gdb高, gdb宽)`（interface.cpp:605，
把 console pty 的 winsize 同步成下半屏实际尺寸）** → 重建 gdb 窗 → 建状态条 →
if_draw。三处尺寸（curses 窗 / vterm 屏 / gdb pty）靠这个顺序保持一致。

**`if_draw()`**（interface.cpp:522-550）：统一「先全部 wnoutrefresh，最后一次
doupdate」的批量刷新（`enum win_refresh`，sys_win.h:29-34）。顺序：filedlg 独占 →
状态条 → 源码窗 → 分隔线 → gdb 窗 → doupdate。

**按键分发** `internal_if_input`（interface.cpp:1516-1592）：按 focus 走
`gdb_input`（867-987，scroll 模式/search 模式，返回 1 表示透传给 gdb）、
`cgdb_input`（1391-1514，`:`/`/`/`?`/Ctrl-W/marks/F 键）→ `source_input`
（1198-1307，滚动/断点）、或 `filedlg_recv_char`。ESC 是 cgdb/gdb 焦点切换键。

**UI → tgdb 的调用点**（UI 主动发命令）：`tgdb_resize_console`（605）、
`tgdb_request_modify_breakpoint`（1188，空格/t 键）、
`tgdb_request_inferiors_source_files`（1268，`o` 键开文件对话框，同时把
`kui_input_acceptable` 置 0）、`tgdb_request_until_line`（1291）、
`tgdb_request_run_debugger_command`（1489-1505，F5-F8/F10）。

### 4.2 sources.cpp —— 源码缓冲与渲染（1400 行）

**数据结构**（sources.h:44-114）：

- `sviewer`：文件链表头 `list_head`（单向链表，`path` 是唯一键，`'*'` 开头表示
  反汇编伪文件）、`cur`/`cur_exe`、`global_marks[26]`+`jump_back_mark`、
  `addr_frame`（当前帧 PC，disasm 模式用）
- `list_node`：每文件一个。`lflags`（`std::deque<line_flags>`，每行断点状态），
  `sel_line/sel_col/exe_line`（0 基行号），`language`，
  `last_modification`（stat 时间戳，自动重载判断）、`local_marks[26]`、
  `addr_start/addr_end`（反汇编节点）
- `buffer`：`lines`（`std::vector<source_line>`，每行 = 文本 + **高亮属性段缓存**
  `std::vector<hl_line_attr>`）、`addrs`、`max_width`、`file_data`（整文件一次
  读入）、`language`（**记录上次高亮用的语言，语言没变就不重新 tokenize**，
  sources.cpp:448）

**加载**：`load_file_buf`（210-290）fopen "rb" 整读 → 按 `\n` 切行 → `load_file`
（299-318）按扩展名推语言 → `source_highlight`（435-464）。

**高亮**：`highlight_node`（358-433）逐行 `tokenizer_set_buffer` →
`tokenizer_get_token` 循环 → `hlg_from_tokenizer_type` 映射到颜色组 → 只在组
类型变化处 push 一个属性段（整行压缩成若干段）。这是 tokenizer 唯一的消费者。

**绘制** `source_display`（760-1065）：每行 `行号 │ 标记 正文` 三段式；断点行行号
用断点色组；执行/选中行按 `displaystyle` 画箭头/highlight/block（977-992）；
hlsearch/incsearch 在正文上做三层属性叠加渲染。滚动：
`source_vscroll`/`source_hscroll`（1083-1110）。

**断点**：`source_set_breakpoints`（1327-1361）先全清再逐个按 path/addr 落
`lflags`。**重载**：`source_reload`（1363-1400）比较 mtime 与 tabstop 变化。
**释放**：`source_free`（1211-1226）；文件节点进程期间常驻内存（不主动淘汰）。

### 4.3 scroller.cpp + vterminal.cpp —— 下半屏终端

**组合关系**（非继承）：`struct scroller` 持有 `VTerminal *vt` 成员
（scroller.cpp:84-85）。vterminal 是纯模型层（libvterm + 自管 scrollback，完全
不知道 curses 窗口的存在）；scroller 是视图层（把 vterm 网格渲染进 SWINDOW）+
交互控制器（scroll 模式、搜索状态机）。

**vterminal.cpp**（650 行）：

- `VTerminal`（20-147）：`VTerm*` + `VTermScreen*` + `sb_buffer`
  （`ScrollbackLine**` 环形队列，**scrollback 是 vterminal 自己实现的，不是
  libvterm 自带**）+ `cursorpos/cursor_visible`
- 构造（171-211）：`vterm_new(h,w)` + utf8 + 注册 `VTermScreenCallbacks`
  （damage/moverect/resize 是空实现，实际只用 movecursor/settermprop/bell/
  sb_pushline/sb_popline）+ `VTERM_DAMAGE_SCROLL` 合并策略
- 写入：`vterminal_write` = `vterm_input_write` + `flush_damage`（231-235）
- 读取：`fetch_row/fetch_cell`（383-461）把 cell 转 UTF-8 + curses 属性；
  256 色索引/默认色映射到 8 色 + bold 表亮色（`get_ncurses_color_index`，
  344-381；RGB 色未支持，377-379 的 TODO）
- Ctrl-L（readline 在 TERM=dumb 下清行）配套的 `push_screen_to_scrollback`
  （491-510，interface.cpp:962-975 注释解释语义）

**scroller.cpp**（664 行）：

- 文件头 1-51 行的 **sid 坐标系**文档：vterm 屏幕 0..h-1、scrollback -1..-sb、
  delta 偏移——搜索用 sid 统一遍历「屏幕+回滚区」，反向搜索用反复左到右取最右
  结果模拟（354-544）
- `scr_new`（157-172）允许窗口为 NULL/零尺寸（`std::max(...,1)` 兜底，
  128-140 注释：下半屏高为 0 时仍要能收 gdb 输出）
- `scr_add` = `vterminal_write`；`scr_move` = 换窗 + `vterminal_resize`
  （vterm 尺寸独立于 curses 窗，gdb pty 侧由 interface 层同步）
- `scr_refresh`（568-664）：逐 cell 画；宽字符 cellwidth 处理；搜索命中叠加；
  scroll 模式顶行画 `[delta/sb_num_rows]`；按焦点显示/隐藏物理光标

### 4.4 highlight_groups.cpp / highlight.cpp —— 颜色体系

- `enum hl_group_kind`（highlight_groups.h:31-73）：22 个语义组（KEYWORD/TYPE/
  COMMENT/EXECUTING_LINE_HIGHLIGHT...）+ 16 个直通色组
- 每组存 `mono_attrs`（无色终端）/`color_attrs`（彩色）/`color_pair`；全局单例
  `hl_groups_instance`（cgdb.cpp:1144 创建）
- 唯一默认主题是 `default_groups_for_background_dark`（96-121），无亮色主题、
  无主题文件——"主题" = 默认表 + 用户 `:highlight` 覆盖
- color pair 管理双路线（441-481 探测）：**ANSI 模式**（色对充足时一次性
  init_pair 全部 64+16 对后查表，305-350）与**惰性分配模式**（每组递增分配 id，
  超过 `swin_color_pairs()` 报错）。移植到 PDCurses 时要决定走哪条
- `hl_line_attr`（highlight_groups.h:200-252）：列号 + 组的轻量封装，是
  sources.cpp 高亮缓存、scroller/filedlg 搜索叠加共用的渲染原语；
  `hl_printline`（1050-1106）按属性段切文本逐段输出
- `:highlight` 解析 `hl_groups_parse_config`（595-791）借用 command_lexer 词法，
  vim 风格 `cterm=bold ctermfg=Red` 语法
- `hl_ansi_get_color_attrs`（888-1019）：独立的 ANSI SGR 解析器，用于 logo 的
  内嵌转义序列（GDB 输出着色不走它，走 vterm cell 属性）
- highlight.cpp：POSIX `regcomp(REG_EXTENDED)` 的惰性编译包装
  （`hl_regex_search/highlight`），被 sources/scroller/filedlg 三方搜索共用

### 4.5 cgdbrc.cpp + command_lexer.lpp —— `:set` 配置机制（1222 行）

三张表：

1. **命令表** `commands[]`（98-140）：`:map/:highlight/:quit/:shell/:syntax` 及
   GDB 直通命令（continue/next/...，action 直接 `tgdb_request_run_debugger_command`）。
   qsort + bsearch 查找
2. **选项默认值表** `cgdbrc_config_options`（156 行起）：23 个选项
   （arrowstyle/disasm/hlsearch/ignorecase/scrollbackbuffersize=10000/tabstop=8/
   timeout 系列/winsplit 系列/wrapscan...）
3. **变量表** `cgdbrc_variables`（284-386）：每个 `:set` 变量的长/短名和类型
   （BOOL/INT/STRING/FUNC_*）

解析链：`:` 命令 → `if_run_command` → `command_parse_string`（1038-1099，
`yy_scan_string` 喂给 command_lexer 的 flex 词法）→ SET token →
`command_parse_set`（887-1035）。

**生效机制的一个坑**：BOOL/INT 型变量的 data 直接指向
`cgdbrc_config_options[].variant`，`:set` 直接写值，**不经过 `cgdbrc_set_val`，
因此不触发 `cgdbrc_attach` 回调**；FUNC_* 型（winsplit/syntax/timeout 等）才走
完整链，且 command_set_* 内部通常直接调 UI 函数立即生效（如
`command_set_winsplit` → `if_set_winsplit`）。目前 attach 机制唯一消费者是
cgdb.cpp:1019-1022 的 KUI 超时联动。

`~/.cgdb/cgdbrc` 由 `command_parse_file`（1101-1134）逐行喂，支持 `\` 续行。

### 4.6 filedlg.cpp / logo.cpp / usage.cpp

- filedlg（673 行）：全屏文件选择。`o` 键 → `tgdb_request_inferiors_source_files`
  → gdb 回 `update_source_files`（cgdb.cpp:577-606）填充并置焦点。排序规则：
  `*` 反汇编伪节点最前、相对路径次之、绝对路径最后（143-174）。
  **特殊点：`capture_regex`（526-582）是 UI 层唯一自旋读键处**——直接
  `kui_ctx->getkey_blocking()`，绕开主循环分发。路径判断是 POSIX 语义
  （`[0]=='/'`），Windows 需重写。**`filedlg_free` 全库无调用点（泄漏）**，
  见 14
- logo.cpp：无文件时画 logo，内嵌 ANSI 转义用 4.4 的 SGR 解析器上色；
  `srand(time(NULL))` 随机 tagline
- usage.cpp：命令行帮助文本

### 4.7 swin 抽象层覆盖度（sys_win.h / sys_win.cpp）

**UI 层零绕过**：`cgdb/` 目录没有任何文件直接 include curses.h（已 grep 验证）。
唯一直接触 curses 的编译单元是 `sys_win.cpp`（条件包含 curses.h /
ncurses/curses.h）。调用计数：interface 40 处、filedlg 51、sources 39、
highlight_groups 26、scroller 22、logo 4、vterminal 0。

覆盖能力清单（sys_win.h:67-195）：初始化/终止（swin_start 含 putenv
ESCDELAY=0、initscr、颜色探测）、终端查询（lines/cols/colors/color_pairs）、
颜色（start_color/init_pair/pair_content/color_pair/use_default_colors 及默认色
扩展探测）、resizeterm、窗口（newwin/delwin）、光标、几何、属性常量
（SWIN_A_*）、输出（werase/wvline/waddch/wclrtoeol/waddnstr/wprintw/mvwprintw）、
刷新（refresh/wrefresh/wnoutrefresh/doupdate）、scrl、keypad、raw、tigetstr、
符号（SWIN_SYM_VLINE/HLINE/LTEE，**HAVE_CYGWIN 时 VLINE 退化为 ':'**）、
COLOR_* 常量。

**缺口**（换后端时需补齐或绕开）：

- **没有输入函数**（wgetch/getch/timeout/nodelay）——键入完全走 KUI 自己读
  stdin，这是移植时必须重造的最大一块
- 没有 wresize/mvwin —— 靠 delwin+newwin 重建
- 没有 wattrset/wbkgd/beep/flash/鼠标
- ESCDELAY putenv 是 ncurses 专有；`swin_supports_default_color_pairs_extension`
  用 `init_pair(65)` 试探，PDCurses 语义不同
- SWIN_CHTYPE 是 unsigned long（sys_win.h:5）；PDCurses 同为 unsigned long 但
  ACS 字符编码不同，需映射
- `swin_getmaxx` 等对 NULL 窗的返回值语义：ncurses 宏返回 ERR=-1，
  scroller.cpp:149 依赖了这一点做兜底

---

## 5. lib/tgdb —— GDB 通信抽象层

tgdb.h 自述定位：前端与底层调试器之间的抽象层。对外 API 全在 tgdb.h（634 行，
注释完整）：`tgdb_initialize/tgdb_start_gdb/tgdb_process/tgdb_send_char/
tgdb_resize_console` + 一组 `tgdb_request_*` + `tgdb_signal_notification`，
事件经 `struct tgdb_callbacks`（tgdb.h:235-356）推回：console_output /
command_response / breakpoints / source_files / disassemble_func /
disassemble_pc / update_file_pos。

### 5.1 tgdb.cpp（1436 行）核心机制

**请求队列**：`struct tgdb`（tgdb.cpp:51-115）持 `command_requests`（list）和
`is_gdb_ready_for_next_command`。所有 `tgdb_request_*` 都是构造一个
`tgdb_request` 结构然后 `tgdb_run_or_queue_request()`（892-916）：

- gdb 忙（非 prompt 态）→ 入队（priority=true 则 push_front）
- **例外：`TGDB_REQUEST_DEBUGGER_COMMAND` 永远立即发**（901-905）——它写的是
  console pty 而不是 MI pty，console 不排队（用户手打的命令都直接透传）
- gdb 回到 prompt（gdbwire 的 prompt 回调 → `tgdb_console_at_prompt`，
  tgdb.cpp:669-678）→ 出队下一条

**命令实际发向**（`tgdb_run_request`，930-962）：MI 请求写 `gdb_mi_ui_fd`，
DEBUGGER_COMMAND 写 `debugger_stdin`（console pty）。

**tgdb_process 的 new-ui 探测**（1084-1131）：console 输出累积找
`Undefined command: "new-ui".`，找到就发带 `new_ui_unsupported=true` 的
TGDB_QUIT。

**初始化时预排的三个请求**（tgdb.cpp:734-749）：当前帧位置（必须先于断点，
否则断点事件没有源文件可挂）、断点表（.gdbinit 里可能预设了断点）、
disassemble /s 模式探测。

### 5.2 MI 命令映射表（tgdb_get_gdb_command，tgdb.cpp:1303-1403）

| 请求 | 实际发给 gdb 的 |
|---|---|
| INFO_SOURCES | `-file-list-exec-source-files` |
| INFO_SOURCE_FILE | `-file-list-exec-source-file` |
| BREAKPOINTS | `-break-info` |
| TTY | `-inferior-tty-set <slavename>`（当前未使用） |
| INFO_FRAME | `-stack-info-frame` |
| DATA_DISASSEMBLE_MODE_QUERY | `-data-disassemble -s 0 -e 0 -- 4`（探测 /s 支持） |
| DEBUGGER_COMMAND | continue/finish/next/.../up/down（人类命令，走 console） |
| MODIFY_BREAKPOINT | `break/tbreak/clear "file":N` 或 `*0xADDR` |
| UNTIL_LINE | `until ...` |
| DISASSEMBLE_PC | `x/Ni $pc` |
| DISASSEMBLE_FUNC | `disassemble [/s 或 /r]` |

**disassemble 的输出不走 MI result，走 console 流**：`gdbwire_stream_record_callback`
里 CONSOLE 流在 DISASSEMBLE 请求期间逐行累积进 `tgdb->disasm`，并解析行首地址
维护 addr_start/addr_end 区间（tgdb.cpp:374-413）。MI 流的 TARGET 输出
（inferior 程序输出）`\n`→`\r\n` 后转给 console_output（415-431）。

**async 事件**（486-504）：`*stopped`/`thread-selected` → 解析 frame 元组触发
`update_file_pos`；断点 created/modified/deleted → 重新请求断点表。

### 5.3 gdbwire.c（8193 行，纯 C）

第三方库（cgdb 作者自己的另一个项目），完整实现 GDB/MI 的词法+语法解析：
`gdbwire_push_data(wire, data, len)` 喂字节流 → 内部状态机切出
stream/async/result/prompt/parse_error 五类记录 → 经 `gdbwire_callbacks`
（gdbwire.h:3044-3100）回调。另有 `gdbwire_get_mi_command()`（gdbwire.h:2420）
把 result_record 匹配成具体 MI 命令的强类型结构（GDBWIRE_MI_BREAK_INFO 等），
`gdbwire_mi_command_free()` 释放。

- **纯 C99，无任何 POSIX 依赖**，Windows 可直接编译（只用了
  stdio/stdlib/stdarg/string）
- tgdb.cpp 用它时大量 `gdbwire_mi_*` 结构（breakpoint 链表、stack_frame、
  source_file 链表），都是 `gdbwire_get_mi_command` 的产出
- 出错路径：gdbwire_parse_error_callback 只打日志（tgdb.cpp:554-564）

### 5.4 tgdb_driver / test.cpp

`lib/tgdb/driver.cpp`（322 行）是手动驱动，**会 fork 真实 gdb 并阻塞等
`new-ui mi`**——孤儿进程问题严重（AGENTS.md 已警告，别进自动化脚本）。
`test.cpp` 是 gdbwire 的独立小测试。

---

## 6. lib/kui —— 按键映射引擎

### 6.1 分层架构（自底向上）

| 层 | 文件 | 职责 |
|---|---|---|
| 键值翻译 | kui_term.cpp/h + kui_cgdb_key.h | 无状态查表：`<Esc>`/`<F1>` 文本 ↔ cgdb_key 枚举 ↔ 终端转义字节序列 |
| 映射条目 | kui_map.cpp/h | 一条 `map lhs rhs`：原始字符串 + 翻译后的 0 结尾 int 数组（key/value 各一份） |
| 映射集 | kui_map_set.cpp/h | `std::map<string, shared_ptr<kui_map>>` 注册表 + kui_tree 匹配 |
| 前缀树 | kui_tree.cpp/h | 有状态匹配自动机，逐键推进 |
| 上下文 | kui_ctx.cpp/h | 读键引擎：驱动 map_set 匹配、缓冲回填、递归展开。**I/O 全靠回调注入，自身零 POSIX** |
| 管理器 | kui_manager.cpp/h | 组合两个 kuictx，对 cgdb 的唯一门面 |

**注意：本项目没有 VK_\* 常量**，对应概念是 `kui_cgdb_key.h` 的 `enum cgdb_key`
（`CGDB_KEY_ESC = 10000` 起，53-116 行），`>= 10000` 与单字节键（0-255）天然
分离，`kui_term_is_cgdb_key` 即按数值区间判断。

### 6.2 kui_tree 前缀树匹配算法

- 节点（kui_tree.cpp:5-34）：`int key` + `kui_map *macro_value`（非空 = 映射在此
  终结）+ `std::map<int, node>` children。子节点键可为任意 int，ASCII 与
  CGDB_KEY_* 混排（`map a<F1>b xyz` 合法）
- `push_key`（148-177）：状态机。查不到 → NOT_FOUND；查到且子节点无后继 →
  立即 FOUND；子节点有 macro_value → 记录 found 但**继续 MATCHING（最长匹配）**
- `finalize_state`（137-141）：输入停止（超时/失配）时若有中途命中则提升为
  FOUND——`map abc` 与 `map abcde` 并存时敲 `abcd` 停手，生效 `abc`
- 剪枝式删除（erase，95-122）：仅无 macro_value 且无 children 才物理删
- **树完全无时钟**；超时由 kuictx 层实现（见 6.3）

### 6.3 kui_manager 的双 kuictx

`kui_manager::create(stdinfd, keycode_timeout, mapping_timeout)`
（kui_manager.cpp:61-68）构造两层：

1. **terminal_keys**（转义层）：map_set 固定为 kui_term 填的终端映射，回调直接
   `io_getchar(stdin)`，超时 = keycode_timeout（典型 40ms）。作用：把
   `ESC [ 23 ~` 折叠成单个 `CGDB_KEY_F11`——**终端键翻译 = 用 kui 自己的前缀树
   匹配转义序列，没有独立解析器**
2. **normal_keys**（用户映射层）：map_set 是热切换的（cgdb 按焦点换
   kui_map/kui_imap），回调嵌套调 terminal_keys->getkey()（kui_manager.cpp:18-44），
   超时 = mapping_timeout（典型 1000ms，vim timeoutlen 等价）

收键路径：`stdin → terminal_keys（折叠转义）→ normal_keys（vim 替换）→
kui_manager::getkey() → cgdb`。`cangetkey()` 取两层缓冲的或。
`getkey_blocking()` 临时把两层超时设 -1（唯一调用者 filedlg.cpp:538）。

`kui_ctx` 的 I/O 通过 `kui_getkey_callback`（kui_ctx.h:40-43）注入——**这是
kui 移植的天然切缝**：换 Console API 只动注入的回调（kui_manager.cpp:5-9 的
char_cb），kui 其余代码不动。但回调签名带 `int fd`，Windows 上需要换成
HANDLE 或做封装。

缓冲回填（update_buffer，kui_ctx.cpp:46-67）：失配/超时后剩余字符 push_front
回缓冲；命中映射把替换结果压最前。**递归映射**由 getkey 的 do-while 实现
（241-256，notes.txt 的设计决策：映射可继续参与匹配）。

### 6.4 kui_term 的三张静态表

- `seqlist`（59-88）：cgdb_key ↔ termcap/terminfo 能力名，进程启动时
  `keyseq_initializer`（297-348）用 `tgetent/tgetstr/swin_tigetstr` 按
  `getenv("TERM")` 填充实际字节序列
- `hard_coded_bindings`（96-161）：方向键 `\033[A`、`\033OA`、控制字符等字面
  序列；**readline 硬编码的序列排最前**（100-111 注释：反查给 readline 的序列
  必须它认识）
- `cgdb_keycodes`（179-278）：`<F11>`/`<C-a>`/`<S-a>` **文本记法**字典，
  `:map` 参数解析器 `kui_term_string_to_key_array`（434-564）用它把 `<...>`
  折叠成单个 int

**移植要点**：Windows 控制台若用 `ReadConsoleInput`，KEY_EVENT_RECORD 自带
虚拟键码——terminal_keys 层整条「字节流→转义匹配→CGDB_KEY」管线可替换为
「VK 码→cgdb_key 直接查表」，seqlist/termcap 初始化整体绕过。表驱动的翻译
部分（cgdb_keycodes、string_to_key_array、hard_coded_bindings 数据）纯数据可保留。

### 6.5 与 readline 的协作（不是依赖）

kui 收键，GDB 进程内的 readline 做行编辑，rline 只在启动时把 readline 的键表
抄给 kui：`init_kui`（cgdb.cpp:1040-1052）对 beginning-of-line / end-of-line /
backward-word / forward-word / kill-word 系列调 `rline_get_keyseq`，注册进
terminal_key_set（动机：发行版 terminfo 坏了但 /etc/inputrc 修好了，跟
readline 的视角保持一致）。发往 GDB 的键经
`kui_term_get_ascii_char_sequence_from_key` 回译成原始字节
（cgdb.cpp:449-461）。

### 6.6 kui_driver

`lib/kui/kui_driver.cpp`：noinst 测试床（dejagnu 就跑它）。`-f 文件` 逐行读
`map lhs rhs`/`unmap`（POSIX 正则解析）；交互态 Ctrl-Z 进映射编辑；其余键打印
键名 + 原始字节序列——调试转义翻译最直观的工具。valgrind 压制文件
kui_driver.supp 全是 ncurses 泄漏误报。

---

## 7. lib/util —— 平台基础设施

**移植主战场**。逐文件：

### 7.1 io.cpp（260 行）—— fd 级读写

- `io_data_ready(fd, ms)`（175-209）：select + timeval，ms=-1 表无限阻塞，
  EINTR 重试，返回 1/0/-1
- `io_getchar(fd, ms, key)`（211-260）：data_ready 后 fcntl 切 O_NONBLOCK 读
  1 字节再恢复——**select-then-nonblocking-read 的写法**，EAGAIN 时返回 c=0
- `io_read`（116-137）：read + EINTR 重试 + **EIO 视为 EOF**（pty 的特点：子进程
  全退后 read master 返回 EIO）
- `io_writen`：write 循环处理部分写
- 消费者：kui 的 char_cb、tgdb_process、主循环 resize/signal 管道读取、
  tgdb_send_char/io_writen 发命令

### 7.2 terminal.cpp（103 行）—— termios 工具

`tty_cbreak`（关 ECHO/ICANON，VLNEXT/VDSUSP 禁用）、`tty_output_nl`、
`tty_off_xon_xoff`（放 ^S/^Q 去被调试程序）、`tty_set_echo`（MI pty 用）、
`tty_get/set_attributes`（保存/恢复）。整个文件就是 termios —— **Windows 上
整体没有对应物**，SetConsoleMode 换掉。

### 7.3 pseudo.cpp（727 行）—— pty 分配

源自 libslack/ssh 的公共领域 pty 代码。对外五个函数（pseudo.h:47-56）：
`pty_open`（master+slave+slavename）、`pty_release`、`pty_set_owner`
（grp/pwd 属主）、`pty_make_controlling_tty`（TIOCSCTTY）、
`pty_change_window_size`（TIOCSWINSZ）、`pty_fork`。

内部多路径分发：优先 `openpty()`（HAVE_OPENPTY，Linux 链 -lutil），退
`/dev/ptmx`（HAVE_DEV_PTMX），再退 `_getpty`/vhangup 等古老 Unix 方案
（pseudo.cpp:70-83 的条件编译矩阵）。`ptsname_r`（HAVE_PTSNAME_R）取从设备名。
Windows 对应物是 ConPTY（`CreatePseudoConsole`，Win10 1809+），**接口形态
（openpty→fd 对）与 ConPTY（句柄对 + 双向管道）不一一对应**，需要一层适配。

### 7.4 fork_util.cpp（275 行）—— pty_pair + 拉起 gdb

- `pty_pair_create/destroy/get_*`：masterfd+slavefd+slavename 三件套的小包装
  （rline 和 tgdb 都用）
- `invoke_debugger`（181-275）：拼 gdb 命令行 → `fs_util_file_exists_in_path`
  验证 → `pty_fork` → 子进程关 MI master 后 `execvp`，父进程关 MI slave。
  gdb 不存在时 exec 失败子进程 exit(0)（父进程靠 console EOF 察觉）
- **Windows 上 execvp/waitpid/kill 模型对应 CreateProcess + 进程句柄等待**

### 7.5 sys_win.cpp/h（340 行）—— curses 抽象层

详见 4.7。实现要点：条件包含三种 curses 头；`swin_start`（52-68）做
putenv ESCDELAY=0 + initscr + 颜色探测；SWIN_SYM_* 运行时初始化（75-82）。

### 7.6 其余

- `clog.h`（664 行）+ cgdb_clog.cpp：单头文件 C 日志库（第三方），三个通道
  id：CLOG_CGDB / CLOG_GDBIO / CLOG_GDBMIIO，输出到 `~/.cgdb/logs/`。
  **32 位 msvcrt 下 printf 别用 %lld/%zu**（AGENTS.md 已警告）
- `sys_util.cpp`：cgdb_malloc/calloc/realloc/strdup（带日志的分配器）、
  `cgdb_close`、`cgdb_hexstr_to_u64`（解析 gdb 的 0x 地址）、`sys_aprintf`
  （vasprintf）、`sys_quote_nonprintables`（日志用）、
  `cgdb_supports_debugger_attach_detection`/`cgdb_is_debugger_attached`
  （/proc/self/status，-w 选项用）
- `fs_util.cpp`：目录创建/拼接/存在性（mkdir -p 语义，POSIX stat 路径，
  Windows 需适配）
- `stretchy.h`：stb 风格 stretchy buffer 宏（filedlg 的 char** 用）
- `cgdbutil_driver`：无参跑 exit 0 的烟测程序

---

## 8. lib/rline —— readline 键表导出（93 行）

极小模块，只干两件事（rline.cpp:23-55 `rline_initialize`）：

1. 建临时 pty_pair，`rl_instream/rl_outstream` 指过去（**让 readline 的终端
   探测不碰真终端**——此刻 ncurses 还没起来），`rl_catch_signals=0`、
   `rl_catch_sigwinch=0`（别和 cgdb 的信号机制打架），调 `rl_initialize()`
   （读 inputrc、建键绑定表），**然后立刻销毁 pty**——pty 只活这几秒
2. `rline_get_keyseq(name, list)`（57-90）：`rl_named_function` →
   `rl_invoking_keyseqs` → `rl_translate_keyseq`，问 readline "某功能绑了哪些键"

用到的 readline 接口全集：rl_instream/rl_outstream/rl_catch_signals/
rl_catch_sigwinch/rl_initialize/rl_named_function/rl_invoking_keyseqs/
rl_translate_keyseq。`rl_readline_state`/`rl_pending_input` 等脏接口**未使用**。

**cgdb 不用 readline 做行编辑**——行编辑是 GDB 进程内 readline 的事；cgdb 侧
readline 只在启动那几秒当"键序列字典"用。**移植捷径：整个模块可 stub 掉**
（硬编码 Home/End/词移动的常见转义序列），避免移植 readline。

mingw32 的 readline/readline.h 有个坑：它不 include stdio.h，直接用会报
`rltypedefs.h:67: unknown type name 'FILE'`，必须先 include stdio.h。

---

## 9. lib/tokenizer —— 语法高亮词法

### 9.1 对外 API（tokenizer.h）

`tokenizer_init/destroy/set_buffer/get_token/get_printable_enum/
get_default_file_type`。token 枚举（8-35）含 KEYWORD/TYPE/LITERAL/COMMENT/
DIRECTIVE 等语义类 + 一批 UI 专用槽位；语言枚举（37-47）：
C/ASM/D/FORTRAN/GO/RUST/ADA/CGDBHELP/UNKNOWN。

### 9.2 语言分发（tokenizer.cpp）

纯运行时函数指针分发，无注册机制：`DECLARE_LEX_FUNCTIONS` 宏（19-23）为每门
语言声明 `X_lex/X_text/X__scan_string/X__delete_buffer` 四个 extern 符号，
`tokenizer_set_buffer` 按语言枚举装进 struct（89-105）。符号名来自每个 .lpp 的
`%option prefix="c_"` 等。后缀→语言表在 7-15（15 种 c 系后缀、.s、.d、.f 系、
.go、.rs、.adb/.ads/.ada），`strcasecmp` 比较。

### 9.3 flex 生成链

8 个 `.lpp`（c/asm/d/fortran/go/rust/ada/cgdbhelp）。automake 内建 `.lpp.cpp`
suffix 规则 + `config/ylwrap`：`flex clexer.lpp` 产 `lex.yy.c`（因 `%option
outfile`），ylwrap 改名为 `clexer.cpp` 再按 C++ 编译。符号前缀靠 `%option
prefix`，与 ylwrap 无关。`AM_LFLAGS` 被注释掉未用
（lib/tokenizer/Makefile.am:5）。生成物全在 .gitignore，仓库里不存在。

`cgdb/command_lexer.lpp` 是另一套独立扫描器（无 prefix，默认 yy* 符号），给
cgdbrc/highlight_groups 解析 `:set`/`:highlight` 用，编进 cgdb/Makefile.am。

**Windows 侧没有 flex**（AGENTS.md 结论），必须在 WSL 预生成 .cpp 拷过来。
生成代码本身跨平台良好（noinput/nounput 已规避常见问题），唯一 POSIX 点是
tokenizer.cpp 的 `strcasecmp`（→ `_stricmp`）。

### 9.4 tokenizer_driver

`tokenizer_driver <文件> <语言>`（两参数必须给全，不给 exit 255）。逐 token
打印类型+文本。已知小 bug：`"rust"` 参数实际映射到 GO（tokenizer_driver.cpp:99-100）。

---

## 10. lib/vterm —— 内嵌终端模拟器

libvterm 0.2（Leonard Evans 作，Neovim 内嵌同款；git 历史
`ab0dcf0`→`005ed6e`）。版本定义 vterm.h:14-15。

| 文件 | 职责 |
|---|---|
| vterm.h | 全部对外 API + 三层回调结构体 |
| vterm.c | VTerm 生命周期、内存分配器、输出缓冲 |
| parser.c | 输入字节流状态机（入口 `vterm_input_write`，parser.c:111） |
| state.c | DEC 终端语义（光标/滚动区/SGR/私有模式），最大文件 |
| screen.c | 屏幕网格（ScreenCell 双缓冲主/备屏）+ damage 合并 + scrollback 推送 |
| pen.c / encoding.c / unicode.c | SGR→颜色、UTF-8 解码、wcwidth（Markus Kuhn） |
| keyboard.c / mouse.c | 把按键/鼠标编回转义序列（**cgdb 未使用**） |

数据流：`vterm_input_write`（gdb 输出的字节）→ parser → state → screen →
用户的 `VTermScreenCallbacks`（damage/movecursor/settermprop/sb_pushline 等，
vterm.h:493-502）。

**纯 ISO C99，零 POSIX 依赖**，Windows 直接编译（注意 MSVC 旧 CRT 的
vsnprintf 语义）。编译警告较多是上游既有状态，别动。

一个冷知识：vterm 的"应答输出"（终端回发的字节）cgdb 既不设回调也不读缓冲，
会被静默丢弃（vterm.c:121-133）——GDB 控制台不需要终端应答，成立。

---

## 11. 构建系统

### 11.1 autogen.sh 与 VERSION

`autogen.sh`：`git rev-parse --short HEAD` 写入 VERSION 文件 →
aclocal/autoconf/autoheader/automake。`configure.ac:3` 的 `AC_INIT` 用
`m4_esyscmd_s([cat VERSION])` 在**生成 configure 时**读入，Makefile 不再读
VERSION。发布时 `CGDB_VERSION=0.8.0 ./autogen.sh` 覆盖。

### 11.2 configure.ac 检查全表（298 行）

- 程序：CC/CXX/INSTALL/RANLIB/`AC_PROG_LEX([noyywrap])`/YACC（YACC 是遗留，
  项目无 .y 文件）
- 平台 case（18-29）：linux→HAVE_LINUX，cygwin→HAVE_CYGWIN
- **强制头**（38-61，缺失即致命）：termios.h、sys/select.h、errno、fcntl、
  **grp.h、pwd.h**、signal、stdarg、stdio、stdlib、string、**sys/ioctl.h**、
  sys/stat、sys/time、time、sys/types、**unistd.h**、ctype、limits、math、regex、
  curses.h（或 ncurses/curses.h）。可选：getopt.h
- 文件：`/dev/ptmx`→HAVE_DEV_PTMX；`/proc/self/status`→HAVE_PROC_SELF_STATUS_FILE
- 函数：putenv/select/strdup/strerror 强制；ttyname_r、ptsname_r（链接探测）
  可选；AC_FUNC_SETPGRP/VPRINTF
- 库：`AC_CHECK_LIB(util, openpty)`→-lutil；**curses 三选一**（195-223）：
  ncursesw → ncurses → curses（最后一个"跑不好"只警告）；**readline**
  （237-255 + config/readline_check_version.m4）：先找 tgetent 宿主
  （libc→ncursesw/ncurses→tinfow/tinfo→curses→termcap），再**编译并运行**
  测试程序读 `rl_library_version`，版本闸门 5.1-9.x
- AM_CONDITIONAL 只有一个：WANT_DOCS（--enable-docs，默认要 makeinfo）
- `AH_BOTTOM` 把 cgdb_custom_config.h 粘到 config.h 尾部；cgdb_custom_config.h
  唯一内容是 ATTRIBUTE_PRINTF 宏

**移植视角**：强制头里 termios/sys/select/unistd/sys/ioctl/grp/pwd 六个在
mingw 下不存在，autotools 流程在 Windows 上走不通（AGENTS.md 已定论需要独立
构建脚本）；readline 的运行期版本探测（AC_RUN_IFELSE）交叉编译时退化。

### 11.3 Makefile.am 结构

全部库是 noinst 静态库，构建顺序 SUBDIRS = util rline tgdb kui tokenizer vterm：

| 目录 | 产物 | 链接位置 |
|---|---|---|
| lib/util | libcgdbutil.a + cgdbutil_driver | 最后（人人依赖它） |
| lib/rline | librline.a | |
| lib/tgdb | libtgdb.a + tgdb_driver | |
| lib/kui | libkui.a + kui_driver | |
| lib/tokenizer | libtokenizer.a + tokenizer_driver | |
| lib/vterm | libcgdbvterm.a | |
| cgdb/ | **cgdb（bin_PROGRAMS）** | LDADD 顺序：tgdb tokenizer kui rline vterm util |
| doc/ | cgdb.info | WANT_DOCS 才进 SUBDIRS |
| testsuite/ | dejagnu，DEJATOOL=kui.base | |

cgdb/Makefile.am 的 DEFS 带 `-DPKGDATADIR`（logo/help 文本运行时定位）和
`-DTOPBUILDDIR`。CXXFLAGS 全项目 `-std=c++11`。

---

## 12. 外部依赖清单

| 依赖 | 用在哪 | 版本要求 | Windows 侧现状 |
|---|---|---|---|
| ncurses(w) | sys_win.cpp 唯一触碰点；全 UI 经 swin_* | ncursesw 优先 | mingw32 有 libncursesw.a（已验证可链） |
| GNU readline | lib/rline 启动期键表导出；GDB 进程内的行编辑（外部依赖） | 5.1-9.x | mingw32 有 libreadline.a；注意 FILE 前置 include 坑 |
| flex | 9 个 .lpp 的生成器 | 2.6.4 验证过 | **没有**，需 WSL 预生成 |
| autoconf/automake | 构建系统 | 2.72（AC_PREREQ） | 没有，需要独立构建脚本 |
| gdb（运行时） | 被调试的前端目标，需支持 `--nw` 和 `new-ui mi` | **>= 7.12** | mingw32 自带 gdb 17.1 |
| texinfo | doc | 可选 --enable-docs | 不需要 |
| termcap/terminfo | kui_term 的 seqlist 填充 | | Windows 控制台无此概念，走 VK 码直查表可绕过 |
| dejagnu/expect | testsuite | | 只有 kui 覆盖 |

语言标准：C11 / C++11（与 GDB 自身一致）。64 位类型用 inttypes.h 的 PRIx64。

---

## 13. POSIX 依赖矩阵（移植风险总表）

按模块汇总全部 POSIX 触点（行号可定位）：

| 模块 | 依赖 | 严重度 | Windows 对应 |
|---|---|---|---|
| lib/vterm | 无 | ✅ 无 | 直接编译 |
| lib/tgdb/gdbwire.c | 无 | ✅ 无 | 直接编译 |
| cgdb/vterminal.cpp | 无直接（颜色链路经 sys_win） | ✅ 低 | |
| lib/tokenizer | strcasecmp ×7 | 低 | `_stricmp` |
| lib/kui 核心（ctx/tree/map_set/manager） | 无（I/O 回调注入） | ✅ 低 | 换注入回调即可；fd→HANDLE 语义渗透到签名 |
| lib/kui kui_term.cpp | termcap/terminfo（tgetent/tgetstr/tigetstr）+ getenv("TERM") | 中 | VK 码直查表方案可整层绕过 |
| lib/rline | readline + pty_pair + fdopen | 中 | 可整体 stub |
| lib/util/io.cpp | select/fcntl O_NONBLOCK/read/write | **高** | WaitForSingleObject + ReadFile（console）/ ReadFile（pty 管道） |
| lib/util/terminal.cpp | termios 全文件 | **高** | SetConsoleMode（raw 等价） |
| lib/util/pseudo.cpp | openpty//dev/ptmx/ptsname/TIOCSCTTY/fork/grp/pwd | **高** | ConPTY CreatePseudoConsole |
| lib/util/fork_util.cpp | pty_fork/execvp | **高** | CreateProcess |
| lib/util/fs_util.cpp | stat/mkdir POSIX 路径 | 低 | _stat/_mkdir 或 CRT 兼容 |
| cgdb/cgdb.cpp 主循环 | select 五路 fd、pipe 自管道、SIGWINCH、read(0) | **高** | WaitForMultipleObjects（管道句柄可进等待集，stdin 不行——核心难点） |
| cgdb/interface.cpp | ioctl TIOCGWINSZ（633、1361）、sigaction 五信号、handler 内 write 管道、struct winsize | **高** | GetConsoleScreenBufferInfo / WINDOW_BUFFER_SIZE_EVENT / 事件对象 |
| cgdb/highlight.cpp | POSIX regex（regcomp REG_EXTENDED） | 中 | std::regex（语法差异）或移植 Spencer regex；被三处搜索共用 |
| cgdb/sources.cpp | stat/st_mtime、fopen rb | 低 | CRT 兼容 |
| cgdb/cgdbrc.cpp | strcasecmp/strncasecmp、flex 词法 | 低 | _stricmp；词法用预生成物 |
| cgdb/filedlg.cpp | 路径 `'/'` 判断 | 低 | Windows 路径规则重写 |
| testsuite | dejagnu/expect/tcl | — | 后置 |

**结构级结论**（与 AGENTS.md 互参）：

1. vterm、gdbwire、kui 的算法核心、tokenizer 数据、UI 逻辑全是可平移的；
2. 真正的墙是**控制台终端基础设施**：pty（fork gdb + ConPTY）、termios
   （raw mode）、select 五路复用（WaitForMultipleObjects 只统一句柄不统一
   fd，且 console 输入不能直接进等待集，惯例做法是专门线程读 stdin 转事件）；
3. resize_pipe/signal_pipe 这种「管道当信号量」的设计在 Windows 上可用
   named pipe / event 对象替代，语义等价；
4. rline 可以 stub，tokenizer 靠 WSL 预生成，flex 生成物跨平台没问题。

---

## 14. 已知陷阱与移植注意点

代码本身的坑（移植时会踩到或可顺手修的）：

1. **信号管道初始化晚于 handler 注册**：`main` 里 `if_init()`（注册信号，
   cgdb.cpp:1159）先于 `init_resize_pipe/init_signal_pipe`（1173-1182）执行。
   窗口期收到 SIGWINCH 会 `write(fd=-1)`，`signal_handler` 里的
   `assert(write(...))`（interface.cpp:765）直接 abort。移植版把管道/事件
   建立挪到信号注册之前可顺带修掉。
2. **filedlg 从不释放**：`filedlg_free` 全库无调用点，`if_shutdown` 也不管
   `fd`，靠进程退出兜底。
3. **BOOL/INT 型 `:set` 不触发 attach 回调**（见 4.5）——给 Windows 版加新
   配置联动时别踩这个语义。
4. **`detab_buffer_str` 返回值被丢**（sources.cpp:246）：Tab 展开实际只对
   反汇编行生效，普通源文件行仍含 Tab，显示层自己消化。
5. **`hl_groups_shutdown` 只置空形参**（highlight_groups.cpp:509-517），全局
   `hl_groups_instance` 悬挂；`:quit` 命令（cgdbrc.cpp:742-747）直接调
   `cgdb_cleanup_and_exit`。
6. **EAGAIN 防御**：cygwin 的 select 假唤醒（cgdb.cpp:837-846 注释）在
   Windows 控制台上大概率重演，保留该容错。
7. **`kui_manager::getkey` 的非阻塞契约**（kui_manager.h:69-77 注释）：调用者
   必须先 select/cangetkey 再调，否则语义是「等最多一个超时窗」。移植版输入
   层重写时保持这个契约，UI 层就不用改。
8. **tokenizer_driver 的 rust→GO 映射 bug**（tokenizer_driver.cpp:99-100）。
9. **clog 在 32 位 msvcrt 上**：printf 系列别用 %lld/%zu（AGENTS.md）。
10. **readline 头的 FILE 前置 include**（mingw32，见第 8 节）。
11. **`interface.cpp:1618` 的 NULL 窗风险**：focus==CGDB 且源码窗高为 0 时
    `swin_wnoutrefresh(src_viewer->win)` 潜在 NULL 解引用，ncurses 宏吞掉，
    PDCurses 行为需验证。
12. **gdb 退出路径靠 EIO**：`io_read` 把 EIO 当 EOF（io.cpp:125-130）——
    pty master 的特有语义，ConPTY 下对应「管道破裂」，要专门处理。

---

## 附：与本文档相关的其他文档

- `AGENTS.md`（仓库根）：工具链、构建命令、Windows 侧探测结论、代码风格
- `doc/porting-feasibility.md`：Windows 移植可行性报告——系统基线（Win10 1809+）、
  GDB new-ui 实测结论与 MI-over-pipes 架构方案、curses 后端选型、ConPTY 接线配方
- `roadmap.txt`：上游作者的架构反思（为什么没有窗口管理器）
- `cgdb/TODO`：历史待办和已知 bug
- `lib/kui/README.md`、`lib/kui/notes.txt`：kui 设计决策
- `doc/cgdb.texi`：用户手册
