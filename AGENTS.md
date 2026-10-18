# AGENTS.md

给 AI 编码代理和新贡献者看的项目工作说明。下面所有命令和版本号都是实际跑过验证的。

## 项目定位

CGDB 是 GDB 的轻量级控制台前端，用 ncurses 做上下分屏（上半屏源码、下半屏 GDB 会话），
按键操作模仿 vim。C11/C++11 项目，和 GDB 本身保持同一标准。

- 上游仓库：https://github.com/cgdb/cgdb
- 上游主页：https://cgdb.github.io/
- 官方源码发布：https://cgdb.me/files/

本仓库是上游的 fork。**长期目标是把只能在 Linux 下运行的 cgdb 移植到 Windows。**
动手前务必先读下面的「Windows 移植」一节，那里列了已经逐项探测过的现状和真实障碍；
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
| `testsuite/` | dejagnu 测试，目前只覆盖 kui |

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

## Windows 移植

**目前 Windows 侧还构建不起来，但可行性已经实测定案。** 完整分析、实验记录和
架构方案见 `doc/porting-feasibility.md`（必读），下面只列影响动手决策的硬结论：

- **最低系统基线：Windows 10 1809（build 17763）+**，因为 ConPTY 从 1809 才有。
  「支持 Windows 10」一律指 1809 及以上。
- **GDB 的 `new-ui mi` 双通道在原生 Windows GDB 上不可用**（mingw32/64 的
  GDB 17.1 实测：挂管道或文件都会在下一个事件循环迭代退出）。移植架构改为
  `gdb --interpreter=mi` 走 stdin/stdout 管道（实测全通，VS Code 同款方案），
  GDB 窗口由 cgdb 自己的行编辑器合成。tgdb/gdbwire 代码不受影响。
- **GDB 本体在 ConPTY 里交互完全正常**（readline、`$1 = 42` 全验证）。
  ConPTY 只用于被调试程序（inferior）的终端；cgdb 自身 UI 渲染到真实控制台。
- **curses 首选 PDCurses-wincon**（`-lpdcurses_wincon -lwinmm`，头文件必须用
  根目录 `pdcurses.h` 包装）：真彩 + Unicode 框线 + CJK 实测通过。ncursesw
  是 8 色备胎（win32 驱动构建，`WriteConsoleOutputW` 码页免疫）。PDCurses-vt
  别用（字节级输出过控制台码页，CP936 下必错码）。
- **本机控制台代码页是 CP936**：字节级写控制台必错码（连 ESC 都可能被吞），
  文本输出要走宽字符 API 或显式 UTF-8 转换，别信 `setlocale("")`。
- ConPTY 接线配方有坑（`UpdateProcThreadAttribute` 传句柄值、必须
  `STARTF_USESTDHANDLES`+NULL 三句柄），细节在 feasibility 文档 3.1 节。
- 32 位 gdb 调不了 64 位程序，主线建议换 `D:\Linux\mingw64` 工具链。

下面是逐项探测过的现状，省得重复踩。

### 工具链

`D:\Dev\mingw32\bin`，target 是 **`i686-w64-mingw32`（32 位）**，gcc 15.2.0（MSYS2 构建），
另有 `mingw32-make` 4.4.1、`windres`、`gdb` 17.1、`pkg-config`。

调用工具前必须把它加进 PATH，**运行编译产物也要加**（否则找不到依赖 DLL），
除非用 `-static`（已验证可用）：

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"     # Git Bash 下的写法
```

配套 vcpkg 在 `E:\Local\vcpkg\installed\x86-mingw-dynamic`（同样 32 位），
需要时把它的 `include`/`lib` 加到 `-I`/`-L`，`bin` 加到 PATH。
注意里面**没有** curses / readline，那两个用 mingw32 自带的。

因为是 32 位 mingw + msvcrt，`printf` 系列**不要用 `%lld` / `%zu`**，
用显式 cast 配 `%ld` / `%lu`。`lib/util/clog.h` 是全套代码的日志出口，改它时尤其注意。

### 依赖库：好消息

mingw32 里已经有 curses 和 readline，这是移植可行的前提：

- `ncurses/curses.h` + `libncursesw.a`（已验证能链接）
- `libpdcurses.a` / `libpdcurses_wincon.a` / `libpdcurses_vt.a` / `libpdcurses_wingui.a`
- `readline/readline.h` + `libreadline.a`（已验证能链接）
- `regex.h`、`getopt.h`、`unistd.h`、`sys/time.h`、`process.h` 都有

mingw32 的 `readline/readline.h` 有个坑：它自己没 include `<stdio.h>`，
直接用会报 `rltypedefs.h:67:29: error: unknown type name 'FILE'`。
**必须在它之前先 `#include <stdio.h>`。**

UI 层已经有一层 curses 抽象 `lib/util/sys_win.h`（`SWINDOW` / `swin_*`，全项目约 280 处调用），
换 curses 后端不需要动业务代码。

### 缺失的 POSIX 接口：真正的工作量

mingw32 里**没有**下面这些头文件，而 cgdb 重度依赖它们：

| 缺失 | 现在用在哪 | Windows 侧对应方案 |
|---|---|---|
| `termios.h` | `lib/util/terminal.cpp` 整个文件（`tcgetattr`/`tcsetattr` 切 raw mode） | `SetConsoleMode` |
| `pty.h` + `fork()` | `lib/util/pseudo.cpp`（`openpty`/`fork`/`TIOCSWINSZ` 拉起 gdb 子进程） | ConPTY（`CreatePseudoConsole`，Win10 1809+） |
| `sys/select.h` | `lib/util/io.cpp:196`、`cgdb/cgdb.cpp:814` 主循环 | `WaitForMultipleObjects` |
| `sys/ioctl.h` | `cgdb/interface.cpp:633` 和 `:1361`（`TIOCGWINSZ` 取窗口尺寸） | `GetConsoleScreenBufferInfo` |
| `sys/wait.h` | 子进程回收 | `WaitForSingleObject` |
| `pwd.h` / `grp.h` | `pseudo.cpp` 里的 pty 属主处理 | Windows 无对应概念，可直接跳过 |
| `curses.h` | 只有 `ncurses/curses.h`，没有裸 `curses.h` | `sys_win.cpp` 已有 `HAVE_NCURSES_CURSES_H` 分支 |

主事件循环是单线程 `select()` 多路复用 5 个 fd：
`STDIN_FILENO`、`gdb_console_fd`（pty master）、`resize_pipe[0]`、`signal_pipe[0]`、`gdb_mi_fd`。
这是移植的核心难点 —— Windows 上没法把这些异构句柄统一进一个等待原语，
`resize_pipe` / `signal_pipe` 这种「用管道当信号量」的写法需要重新设计。

### 缺失的构建工具

- **flex 在整个 Windows 侧都不存在**（mingw32 没有，Git Bash 没有，scoop 里只有个 2.4.1 的老 bison）。
  而 `lib/tokenizer/` 有 8 个 `.lpp`、`cgdb/command_lexer.lpp` 有 1 个，
  生成物（`clexer.cpp` 等）全在 `.gitignore` 里，仓库中不存在。
  → 必须在 WSL 侧用 flex 预生成，再把 `.cpp` 拷进 Windows 构建树。
  automake 把 `.lpp` 识别为 C++ lex 源文件，规则是内建的，项目里没有显式写。
- **autoconf / automake 都没有**，只有 `mingw32-make` 4.4.1。
  → autotools 流程在 Windows 上跑不通，移植版需要一套独立的构建脚本
  （手写 Makefile 还是 CMake 尚未决定，别擅自选定）。
- 项目里没有任何 `.y`/`.ypp` 文件，`configure.ac` 里的 `AC_PROG_YACC` 是历史遗留，**不需要 bison**。

### 移植时的注意事项

- 不要为了迁就 Windows 去改 `configure.ac` 的 `AC_PREREQ([2.72])`，那是上游 commit `ae240a2` 定的。
- 平台差异优先收敛到 `lib/util/` 里，用 `#ifdef` 分支或新增 `*_win.cpp`，
  不要把 Windows 特例散落到 `cgdb/` 的 UI 代码里。
- 上游代码风格由 GNU indent + 仓库根的 `.indent.pro` 定义，改完 C/C++ 代码按它跑一遍 indent。

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
- `doc/porting-feasibility.md` —— **Windows 移植可行性报告（本仓库自写，实测背书）**：
  系统基线（Win10 1809+）、GDB new-ui 不可用的实测证据、MI-over-pipes 替代架构、
  curses 三后端能力矩阵、ConPTY 接线配方、自写 swin 后端兜底分析、阶段计划。
  动手移植前必读
- `roadmap.txt` —— 上游作者写的架构反思和待办，解释了为什么 cgdb 至今没有窗口管理器
- `cgdb/TODO` —— 更细的历史待办和已知 bug 清单
- `NEWS` —— 完整的版本变更历史
- `FAQ`、`INSTALL` —— 上游的用户向文档
