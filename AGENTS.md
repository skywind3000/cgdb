# AGENTS.md

给 AI 编码代理和新贡献者看的项目工作说明。下面所有命令和版本号都是实际跑过验证的。

## 项目定位

CGDB 是 GDB 的轻量级控制台前端，用 ncurses 做上下分屏（上半屏源码、下半屏 GDB 会话），
按键操作模仿 vim。C11/C++11 项目，和 GDB 本身保持同一标准。

- 上游仓库：https://github.com/cgdb/cgdb
- 上游主页：https://cgdb.github.io/
- 官方源码发布：https://cgdb.me/files/

本仓库是上游的 fork。**长期目标是把只能在 Linux 下运行的 cgdb 移植到 Windows——已完成**：
`windows` 分支上有全功能原生版（构建/用法见下面「Windows 移植」一节，实施记录见
`doc/win_specs.md`），master 保持与上游同步；
需要理解代码内部机制（主循环、数据流、各模块原理）时，读 `doc/reference.md`。

## 临时文件约定

| 环境 | 目录 |
|---|---|
| Windows 侧（本仓库工作区） | `<项目根>/.tmp/` |
| WSL2 侧 | `/home/skywind/tmp` |

`.gitignore` 第 52 行的 `/.*/*` 已经覆盖了 `.tmp/`，不用额外配置。
构建产物、日志、探测脚本、下载的源码包一律放这两个地方，不要散落在仓库里。

## 代码结构

| 路径 | 职责 |
|---|---|
| `cgdb/` | 主程序与 UI。`cgdb.cpp` 是入口和主事件循环，`interface.cpp` 负责分屏渲染，`scroller.cpp` 是滚动窗口基类，`sources.cpp` 管源码缓冲区，`highlight*.cpp` 做语法高亮上色，`filedlg.cpp` 是文件选择对话框，`cgdbrc.cpp` 处理 `:set` 配置项，`vterminal.cpp` 是内嵌终端，`command_lexer.lpp` 是命令行词法 |
| `lib/tgdb/` | tgdb = 与 GDB 通信的抽象层。`tgdb.cpp` 是主体，`gdbwire.c` 解析 GDB 的 annotate 输出流 |
| `lib/kui/` | kui = key user interface，vim 风格的按键映射引擎（`map`/`imap`），用前缀树匹配多键序列。数据结构说明见 `lib/kui/README.md` 和 `notes.txt` |
| `lib/vterm/` | 内嵌的 libvterm 终端模拟器，第三方 C 代码，编译警告较多，一般不用动 |
| `lib/tokenizer/` | flex 生成的各语言词法分析器，供语法高亮用。8 个 `.lpp`：c / asm / d / fortran / go / rust / ada / cgdbhelp |
| `lib/rline/` | GNU readline 的封装，把 readline 的按键绑定导出给 kui |
| `lib/util/` | 平台相关基础设施，**移植工作的主战场**：`pseudo.cpp`(pty)、`terminal.cpp`(termios raw mode)、`io.cpp`(select 封装)、`fork_util.cpp`、`sys_win.cpp`(curses 抽象层)、`fs_util.cpp`、`clog.h`(单头文件日志库) |
| `doc/cgdb.texi` | 用户手册源文件（texinfo）；同目录的 `reference.md` 是本仓库自写的源码参考手册 |
| `testsuite/` | dejagnu 测试，目前只覆盖 kui；`testsuite/win32/` 是 Windows 版的 ConPTY 端到端套件 |
| `python/` | **CGDB 的独立 Python 实现**（prompt_toolkit + GDB/MI），不参与 C 构建，自带 pytest 套件。入口 `python/cgdb.py`，说明和踩坑清单见 `python/README.md` |

`cgdb/tester.cpp` **没有被任何 Makefile.am 引用，不参与构建**，是历史遗留文件，别照着它改。

## Linux 原生构建（WSL2 Ubuntu-24.04）

### 环境

已经装好，关键版本：

- gcc / g++ 13.3.0，GNU make，flex 2.6.4，bison，automake 1.16.5，libtool 2.4.7
- **autoconf 2.72 装在 `~/.local/bin`** —— Ubuntu 24.04 仓库只有 2.71，而 `configure.ac`
  写了 `AC_PREREQ([2.72])`，用系统自带的会直接报
  `configure.ac:2: error: Autoconf version 2.72 or higher is required`。
  源码包从 `https://ftp.gnu.org/gnu/autoconf/autoconf-2.72.tar.gz` 编译安装，
  构建树留在 `/home/skywind/tmp/autoconf-bootstrap`，需要重装时照抄即可。
- gdb 15.1，texinfo(makeinfo)，dejagnu(runtest)，indent
- ncursesw / readline 开发包（`libncursesw`、`libreadline`、`libtinfo`、`libutil`）

**所以每条命令前都要 `export PATH=$HOME/.local/bin:$PATH`。**

### 从 Git Bash 调 WSL 的坑

- 必须加 `MSYS_NO_PATHCONV=1`，否则 `/mnt/e/...` 会被 Git Bash 改写成
  `C:/Program Files/Git/mnt/e/...` 然后报文件不存在
- `wsl.exe` 的输出是 UTF-16，管道后面要接 `| tr -d '\0'` 才能读
- 复杂脚本写成文件放 `.tmp/`，再 `wsl.exe -d Ubuntu-24.04 -- bash /mnt/e/.../xxx.sh`。
  不要用 `bash -lc '...'` 内联传脚本，`$var` 会在半路被吃掉
- 从 Windows 侧 kill 掉 `wsl.exe` **不会**终止 WSL 里已经跑起来的进程，会留孤儿

### 构建命令（已验证，全量约 3 秒）

```bash
export PATH=$HOME/.local/bin:$PATH

# 1. 生成 configure。在源码树里跑，产物都已被 gitignore，git status 保持干净
cd /mnt/e/Github/Tools/cgdb
./autogen.sh

# 2. 目录外构建。build 目录必须放 ext4 上，/mnt/e 是 9p 挂载，慢很多
mkdir -p /home/skywind/tmp/cgdb-build && cd /home/skywind/tmp/cgdb-build
/mnt/e/Github/Tools/cgdb/configure \
  CFLAGS="-std=c11 -g -O0 -Wall -Wextra -Wshadow -pedantic -Wno-unused-parameter" \
  CXXFLAGS="-std=c++11 -g -O0 -Wall -Wextra -Wshadow -pedantic -Wmissing-include-dirs -Wno-unused-parameter -Wno-sign-compare -Wno-unused-but-set-variable -Wno-unused-function -Wno-variadic-macros"
make -srj$(nproc)
```

产物是 `cgdb/cgdb`（ELF x86-64）。版本号取自源码树根的 `VERSION` 文件，
由 `autogen.sh` 用 `git rev-parse --short HEAD` 生成。

冒烟验证：`./cgdb/cgdb --version` 应输出 `CGDB <short-hash>`。

`lib/vterm/` 会产出一批 `-Wmissing-field-initializers` / `-Wsign-compare` 警告，
是上游第三方代码的既有状态，不是回归。

### 测试

```bash
cd /home/skywind/tmp/cgdb-build && make check
```

dejagnu + expect 驱动 `lib/kui/kui_driver`，跑 `testsuite/kui.base/basic.exp`，
结果写在 `testsuite/kui.base.sum`。

**基线：198 项全部通过。**

已知 flaky：偶尔会出现 1 项 `FAIL: timeout testing map from mapping file`，
原因是 `testsuite/config/unix.exp` 里 `set timeout 3` 太紧，重跑即过。别当成回归去追。

只有 kui 有自动化测试覆盖。tgdb / tokenizer / vterm / UI 层都没有。

### 运行和调试

cgdb 是全屏 curses 程序，**必须有真实 tty**。用 `wsl.exe -- bash -c` 这种非交互方式跑会直接卡死
（stdin 不是终端，而且不加 `</dev/null` 时会阻塞在读 stdin 上）。要交互运行就在
Windows Terminal 里开一个 WSL 标签页：

```bash
export PATH=$HOME/.local/bin:$PATH
cd /home/skywind/tmp/cgdb-build
./cgdb/cgdb -- -q ./someprogram     # -- 之后是原样传给 gdb 的参数
```

调试 cgdb 自身：`gdb --args ./cgdb/cgdb ...`。

### 各模块的 driver 程序

每个 lib 都编出一个 `noinst_PROGRAMS` 的手工驱动，是这套代码事实上的单元测试入口：

| 程序 | 用法 |
|---|---|
| `lib/util/cgdbutil_driver` | 无参数直接跑，exit 0 |
| `lib/kui/kui_driver` | `[映射文件]`，交互式，dejagnu 就是驱动它 |
| `lib/tokenizer/tokenizer_driver` | **必须给两个参数**：`<文件> <语言>`，语言取 `c`/`asm`/`d`/`fortran`/`go`/`rust`/`ada`。不给参数 exit 255 |
| `lib/tgdb/tgdb_driver` | 见下面的警告 |

**`tgdb_driver` 会 fork 出一个真实的 `gdb --nw -ex new-ui mi /dev/pts/N` 并阻塞等待。**
`timeout` 只能杀掉 driver 本身，gdb 孙进程会变成孤儿继续占着 pty，
外层 shell 因为管道没关而一直挂着。不要把它塞进自动化脚本里无脑跑。已经跑飞了就这样清理：

```bash
pkill -f tgdb_driver; pkill -f "new-ui mi"
```

## Windows 移植（已完成，分支 `windows`）

**Windows 原生版已完成并可日常使用**（在 `windows` 分支上开发，master 不受影响）。
构建产物是全静态的 `cgdb.exe`（仅依赖系统 DLL），E2E 23 项 + MI driver 11 项 +
Linux 回归 198 项全绿。**实施规格、全部踩坑记录和测试报告见 `doc/win_specs.md`（必读）**；
可行性论证见 `doc/porting-feasibility.md`。影响使用的硬结论：

- **最低系统基线：Windows 10 1809（build 17763）+**（ConPTY 下限；实际只在
  inferior 终端间接依赖，主 UI 只要 VT 输入模式，1607+）。
- **GDB 的 `new-ui mi` 双通道在原生 Windows GDB 上不可用**（GDB 17.1 实测致命）。
  实际架构是 `gdb --interpreter=mi` 走匿名管道（VS Code 同款），GDB 窗口由
  tgdb 内嵌的行编辑器合成（历史/光标编辑/ESC 键序列都有，无 Tab 补全）。
- GDB 用 `D:\Dev\mingw32\bin\gdb.exe`（32 位，只能调 32 位 inferior；
  换 64 位 gdb 用 `-d` 指定即可，cgdb 本身跨位数）。
- **curses 用 PDCurses-wincon 静态链接**（mingw32 自带，真彩 + CJK 实测通过）。

### Windows 构建与测试（Git Bash，仓库根目录）

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"
mingw32-make -f win32/Makefile              # → .tmp/win-build/cgdb.exe
mingw32-make -f win32/Makefile drivers      # 单元 driver
mingw32-make -f win32/Makefile check        # tokenizer 六语言喂源文件
```

运行（必须从仓库根目录，`:help` 相对路径需要）：`.tmp/win-build/cgdb.exe -- -q prog.exe`
（`--` 之后原样传给 gdb）。日常用法和 Linux 相同：`:set`、空格切换断点、
F5/F8/F10、ESC 切窗口、`:q` 退出；^C 中断 inferior。

E2E 测试：`.tmp/e2e_cgdb.py`（ConPTY 里跑真 cgdb + VT 屏幕解析断言，23 项）；
MI 传输验收：`.tmp/mi_driver.cpp`（basic 7/7 + interrupt 4/4）。

**全覆盖测试套件（改完 Windows 侧代码后必跑，约 3 分钟）**：

```bash
python testsuite/win32/run_tests.py     # 96 项，CLI/焦点/状态条/调试/导航/resize/文件/:命令/退出
```

覆盖说明：按键用逐字节带间隔发送（真实打字节奏，每个键都产生
KEY_DOWN/KEY_UP 记录对——`testsuite/win32/README.md` 有完整矩阵）。
其中「状态条 `:` 后静置」是 KEY_UP 幽灵信号崩溃的回归用例。

### 关键实现事实（改代码前必知）

- **所有 Windows 差异都收在 `#ifdef _WIN32` 里**，Linux 构建零改动
  （回归 198/198 验证过）。平台设施集中在 `lib/util/console_win.*`
  （控制台模式/VT 输入/尺寸轮询）和 `lib/util/process_win.*`
  （gdb 管道 + inferior 命名管道 + 读线程）。
- **匿名管道读句柄在本机 WaitForSingleObject 不可靠**（空管道也返回就绪）：
  io 层一律 PeekNamedPipe 轮询；主循环不能把 MI 管道放进等待集。
- **CRT read() 在信号化空管道上会永久阻塞**：io_read/io_writen 走 ReadFile/
  WriteFile 原始句柄；`ERROR_BROKEN_PIPE` 即 EOF。
- **控制台句柄会被非按键记录（焦点事件、KEY_UP 等）信号化**，ReadFile 却只认
  key-down 记录：读之前用 PeekConsoleInput 门禁并排掉非按键记录，否则 kui
  超时契约被挂死。**io_data_ready 必须循环等到真有 key-down 或超时**（首发版
  排完记录就返回 0，被 kui 的 findkey 当成 -1，按 ':' 进状态条后松键即
  静默退出——KEY_UP 记录是每键必有的）。主循环的用户输入门禁统一走
  io_data_ready(0,0)，别信裸 WaitForMultipleObjects 结果。
- **同步命名管道上读线程常驻 ReadFile 时，主线程 WriteFile 同句柄会死锁**：
  inferior 管道全部重叠 I/O。
- **构建必须用 `-iquote` 引项目头**：`-I lib/util` 会让 CRT 的 `<io.h>`
  被 cgdb 的 `lib/util/io.h` 遮蔽（mingw `<unistd.h>` 会引入 `<io.h>`）。
- swin 属性编码：PDCurses 宽字符 chtype 是 64 位，颜色位在 bit 44+；
  Windows 侧保持 int API（低 8 位 SWIN_A_*、bit 20+ 存颜色对+1，
  wattron/wattroff 边界展开）——**别把裸 curses 属性值塞进 int**。
- flex 生成物提交在 `win32/generated/`，改 `.lpp` 后跑
  `win32/regen-lexers.sh`（WSL 里）重生成。
- 32 位 msvcrt：`printf` 别用 `%lld`/`%zu`（cast 配 `%ld`/`%lu`）。

### 探测期历史结论（已完成使命，仅供追溯）

以下在可行性阶段逐项实测定案，具体证据都在 porting-feasibility.md：

- GDB 在 ConPTY 里交互完全正常（readline、求值全验证）；ConPTY 只用于
  inferior 终端，cgdb 自身 UI 渲染到真实控制台。
- 本机控制台码页 CP936：字节级写控制台必错码；PDCurses 走宽字符 API 免疫。
- ConPTY 接线配方三坑（属性表传句柄值、USESTDHANDLES+NULL、ClosePseudoConsole
  会阻塞），细节在 feasibility 文档 3.1 节。
- ncursesw 是 8 色备胎（win32 驱动构建）；PDCurses-vt 别用（码页问题 + 崩溃）。

## 代码风格

`.indent.pro` 是权威定义，要点：4 空格缩进、**不用 tab**、行宽 80（注释也是 80）、
`if`/`struct` 的左花括号不换行、`else` 紧贴、`case` 缩进 4、续行缩进 8、cast 后加空格。

## 提交规范

上游要求每个 patch 带 signed-off-by（Developer's Certificate of Origin，见 `CONTRIBUTING.md`）：

```bash
git commit -s
```

往上游提 PR 前要先按 `CONTRIBUTING.md` 的流程和维护者讨论。
本仓库自己的 Windows 移植工作不受这条约束，但保持 `-s` 的习惯没坏处。

## 其他有用的文件

- `doc/reference.md` —— **源码参考手册（本仓库自写，Windows 移植底稿）**：架构总图、
  四条核心数据流、启动/关闭时序、逐模块代码原理（含行号引用）、构建系统与依赖分析、
  POSIX 依赖风险矩阵、已知代码陷阱清单。做移植设计或改核心代码前先读它，
  本文（AGENTS.md）只覆盖目录级概览和工具链，深入细节以 reference.md 为准
- `doc/porting-feasibility.md` —— **Windows 移植可行性报告（实测背书）**：
  系统基线（Win10 1809+）、GDB new-ui 不可用的实测证据、MI-over-pipes 替代架构、
  curses 三后端能力矩阵、ConPTY 接线配方、自写 swin 后端兜底分析
- `doc/win_specs.md` —— **Windows 移植实施规格与实测记录（分支 `windows` 的权威文档）**：
  每一步的决策依据、四个致命管道/控制台陷阱及修复、构建系统、平台层逐文件细节、
  tgdb 传输层改造、合成 GDB 窗口设计、E2E 测试报告（23/23）、已知限制清单。
  在 windows 分支上改代码前必读
- `doc/python_version.md` —— **Python 版可行性分析（实测背书）**：三条架构路线的取舍、
  TUI 选型实测对比（prompt_toolkit vs Textual）、模块映射与工作量估算、
  MI/Pygments/启动延迟的实测数据、硬约束清单。`python/` 目录的设计依据
- `roadmap.txt` —— 上游作者写的架构反思和待办，解释了为什么 cgdb 至今没有窗口管理器
- `cgdb/TODO` —— 更细的历史待办和已知 bug 清单
- `NEWS` —— 完整的版本变更历史
- `FAQ`、`INSTALL` —— 上游的用户向文档
