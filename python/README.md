# CGDB (Python)

`cgdb.py` 是 CGDB 的 Python 实现：同样的上下分屏、同样的 vim 式按键、同样驱动
一个真实的 gdb 子进程。它不是 C 版的封装，而是按 `doc/python_version.md` 的
分析重写的独立实现。

设计依据、实测数据和取舍理由都在 **`doc/python_version.md`**，本文只讲怎么用、
怎么改、怎么测。

## 运行

```bash
# 依赖（本机已装）
pip install prompt_toolkit pygments

# 最简用法：-- 之后原样传给 gdb
python python/cgdb.py -- -q ./myprogram

# 指定 gdb（Windows 上通常需要）
python python/cgdb.py -d D:\Dev\mingw32\bin\gdb.exe -- -q prog.exe

# 启动就打开某个源文件
python python/cgdb.py --source src/main.c -- -q prog.exe
```

命令行接口对齐 C 版：

| 选项 | 含义 |
|---|---|
| `--version` | 打印版本后退出 |
| `--help` | 打印帮助后退出 |
| `-d DEBUGGER` | 指定调试器（默认 PATH 里的 `gdb`） |
| `-w` | 等待调试器 attach 后再启动 |
| `--no-color` | 关闭语法高亮和颜色 |
| `--source FILE` | 启动时在源码窗打开 FILE |
| `--` | CGDB 选项结束，之后原样传给 gdb |

`-d` 指向不存在的文件时会**立即报错退出**并说明原因，不会静默崩溃
（C 版曾经有这个 bug，见 commit "Fix silent crash when gdb is not on PATH"）。

## 按键

| 键 | 作用 |
|---|---|
| `ESC` | 切到源码窗（`cgdbmodekey` 可改） |
| `i` | 切到 GDB 窗 |
| `s` | 切到 GDB 窗并进入滚动模式 |
| `F5` / `F6` | run / continue |
| `F7` / `F8` / `F10` | finish / next / step |
| `Ctrl-C` | 中断 inferior |
| `Ctrl-L` | 重绘 |
| 空格 | 在光标行切换断点 |
| `t` | 临时断点（tbreak） |
| `j` `k` `G` `gg` `Ctrl-D` `Ctrl-U` `Ctrl-F` `Ctrl-B` | 源码窗移动 |
| `m<x>` / `'<x>` | 设置 / 跳转到标记 |
| `/` `?` `n` `N` | 向下 / 向上搜索、重复 |
| `+` `-` | 增减源码窗高度 |
| `:` | 命令行 |
| GDB 窗内 `PageUp`/`PageDown` | 进入滚动模式翻历史输出 |

GDB 窗内是普通的行编辑：方向键翻历史、`Ctrl-A/E/U/K/W` 等 readline 键位都由
prompt_toolkit 的 Buffer 提供，不需要自己实现。

### `:` 命令

| 命令 | 说明 |
|---|---|
| `:set <opt>[=<value>]` | 读写配置项，`:set all` 列出全部 |
| `:open <file>[:line]` | 打开源文件 |
| `:map <lhs> <rhs>` / `:unmap <lhs>` | 增删按键映射（`:imap` / `:iunmap` 同理） |
| `:clear` | 清空 GDB 窗 |
| `:help` | 按键速查 |
| `:q` | 退出 |
| 其他 | 原样当作 gdb 命令执行 |

配置项名称、缩写、类型和默认值全部照抄 `cgdb/cgdbrc.cpp`，
`tests/test_cgdbrc.py` 会**直接从 C 源码里正则抓出选项表来比对**，防止漂移。
也读 `~/.cgdbrc` 和 `./.cgdbrc`。

## 架构

```
python/
  cgdb.py                  入口、命令行解析、rc 文件加载
  cgdbpy/
    mi.py                  GDB/MI 记录解析（对应 gdbwire.c 的 8193 行）
    gdbclient.py           gdb 子进程 + token 化请求/响应 + 回调分发
    inferior_tty.py        inferior 的终端：Windows 命名管道 / POSIX pty
    kui.py                 vim 式按键映射引擎（对应 lib/kui）
    keycodes.py            键值常量与 <...> 记法表（对应 kui_term.cpp）
    console.py             GDB 窗行模型（对应 scroller_win.cpp）
    source.py              源码缓冲、Pygments 高亮缓存、断点表
    highlight.py           Pygments token -> prompt_toolkit 样式
    cgdbrc.py              :set 配置项（对应 cgdbrc.cpp）
    app.py                 prompt_toolkit 界面与事件循环
  tests/                   pytest 套件
  tests/e2e/               ConPTY 端到端测试
```

数据流和 C 版一致（`doc/reference.md` §1.4），但传输层用的是 Windows 移植版
的形态（`doc/win_specs.md` §7）：

```
gdb --nw -q --interpreter=mi   ← 匿名管道，单通道
   ↑ -interpreter-exec console "用户输入的命令"
   ↓ ~/@/& 流记录、*stopped 等异步记录、^done 结果记录
gdbclient（读线程解析 + 派发线程回调 + 泵线程发命令）
   ↓ 回调
app.py（prompt_toolkit 渲染）
```

inferior 的 tty 单独给（`-gdb-set inferior-tty`），否则它的 stdout 会灌进 MI
管道把记录流打乱 —— 这是 C 版 M0 阶段实测出来的（`win_specs.md` §4 E2a）。

## 改代码前必知的几个坑

这些都是开发过程中实测踩到的，每一个都有对应的回归测试。

1. **`-gdb-set inferior-tty` 的参数绝对不能加引号。**
   加了引号 gdb 会回 `^done`（假装成功）但实际没打开设备，inferior 输出直接
   漏进 MI 管道。四种写法实测：不加引号的反斜杠和正斜杠都能用，加引号的三种
   全废。见 `inferior_tty.py` 的 `gdb_argument()` 和
   `test_gdb_integration.py::test_inferior_tty_must_not_be_quoted`。

2. **`-break-insert` 的 location 必须把 `路径:行号` 整体作为一个 MI 字符串加引号。**
   `-break-insert -- "C:\dir\f.c":12`（只给路径加引号，也就是 C 版在 console
   侧用的写法）在 MI 下会报 `Problem parsing arguments`。
   用 `mi.breakpoint_location()`，别自己拼。

3. **`-break-insert` 不会发 `=breakpoint-created` 通知。**
   断点只在 `^done,bkpt={...}` 结果里。等通知会一直等不到，gutter 上的 `B`
   标记要等到下一次停住才出现。用 `Breakpoints.absorb_result_bkpt()`。
   反过来，用户在控制台敲 `break` **会**发通知。

4. **`=breakpoint-deleted` 的载荷是 `id="1"`，不是 `bkpt` 元组。**
   按 `bkpt` 取会静默忽略所有删除。

5. **回调里可以安全地调用阻塞的 `call()`。**
   曾经不行：回调跑在读线程上，而 `_on_stopped` 会调 `-break-info` 刷新断点表，
   `call()` 阻塞等结果记录 —— 而结果记录只能由这个被阻塞的读线程送达。
   第一次 `*stopped` 就把整个会话冻死，MI 日志停在 `6-break-info`。
   现在「配对请求/响应」留在读线程，用户回调在独立派发线程上跑。
   回归测试：`test_gdb_integration.py::test_callback_may_issue_a_blocking_call`。

6. **`Application.ttimeoutlen` 必须设成 cgdb 的 100ms。**
   prompt_toolkit 在 VT 输入模式下会用 `Vt100Parser` 扣住 lone ESC 等后续字节，
   只有 `ttimeoutlen` 到点才 flush，而它的默认值是 **0.5 秒**。ESC 是 cgdb 的
   切窗键，实测延迟 499ms（普通键 0–3ms）。接上 cgdb 自己的 `ttimeoutlen`
   （默认 100ms）后降到 107ms。
   回归测试：`test_e2e.py::test_key_latency_is_not_the_500ms_escape_bug`。

7. **`Keys.Escape` 的绑定必须 `eager=True`。**
   否则 prompt_toolkit 认为它可能是更长序列的前缀，一直不触发。
   实测：不加 eager 时 ESC 永远不到达 handler。

8. **`KeyPressEvent` 没有 `key_press` 属性**，要用 `event.key_sequence[-1]`。
   写错的表现是**每个按键都静默失败**（异常被 ptk 吞掉，界面毫无反应），
   单元测试也发现不了 —— 因为单元测试直接调 `on_key()` 传的是假对象。
   只有 ConPTY 端到端测试能抓到。

9. **窗格内容不要按固定 budget 补齐行数。**
   prompt_toolkit 从顶部开始显示 formatted text，在顶部补空行会把真实内容
   推出可视区（表现为 GDB 窗一片空白）。按实际可见高度生成，需要底部对齐时
   在顶部补齐**恰好** `可见高度` 行。

10. **不能只 lex 可见窗口。**
    C/C++ 的 token 能跨几千行，窗口起点落在块注释内部时整屏染色全错。
    实测 `lib/tgdb/tgdb.cpp` 上 11 个 40 行窗口有 6 个与全文 lex 不一致，
    最差的 40 行里错 36 行。所以 `source.py` 全文 lex 一次 + 按行缓存，
    大文件走后台线程。
    测试：`test_source.py::test_windowed_lexing_is_demonstrably_wrong`。

## 测试

```bash
# 全部（单元 + 真实 gdb 集成 + ConPTY 端到端），约 2.5 分钟
cd python
python -m pytest tests/ -q

# 只跑快的单元测试（不碰 gdb，不起 ConPTY），约 2 秒
python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/test_gdb_integration.py

# 只跑端到端
python -m pytest tests/e2e -q
```

集成测试和端到端测试需要 `gdb` 和 `gcc`。Windows 上把
`D:\Dev\mingw32\bin` 加进 PATH 即可；也可以用 `CGDB_GDB` 指定 gdb 路径。
缺依赖时这些测试会 skip，不会 fail。

| 测试文件 | 覆盖 | 数量级 |
|---|---|---|
| `test_kui.py` | 按键映射引擎，**移植自 `testsuite/kui.base/basic.exp` 的 198 项**，并与真实 C `kui_driver` 的黄金输出逐用例交叉验证 | ~360 |
| `test_mi.py` | MI 记录解析：文法、C 转义（含八进制 `\033`）、嵌套、畸形输入降级、分块边界 | ~70 |
| `test_console.py` | GDB 窗行模型的字节语义，逐条对应 `win_specs.md` §8.1 | ~55 |
| `test_source.py` | 源码加载/编码、高亮缓存正确性、窗口 lex 反证、断点表 | ~45 |
| `test_cgdbrc.py` | `:set` 选项，**与 `cgdb/cgdbrc.cpp` 的选项表交叉比对** | ~55 |
| `test_app.py` | UI 层：焦点、导航、断点、功能键、命令行、渲染，用假 gdb | ~80 |
| `test_gdb_integration.py` | **真实 gdb**：启动、token 配对、断点/运行/单步/求值、inferior tty、退出清理 | ~25 |
| `e2e/test_e2e.py` | **真实 cgdb.py + 真实 gdb 跑在真实 ConPTY 里**，VT 屏幕解析断言 | ~22 |

### kui 的黄金参考

`tests/data/kui_oracle.json` 是**用真实的 C `kui_driver` 跑出来的**，不是我自己
写的期望值。生成方式：在 pty 里启动 `lib/kui/kui_driver --file <map>`，按
`basic.exp` 的顺序喂同样的 198 组输入，逐用例抓取它的 stderr。

`test_kui.py` 两种用法都做了：
- 全序列比对：把 198 个用例的输出连起来（去掉 `(kui) ` 提示符）与 C 版逐字符比
- 逐用例比对：189 个「干净」用例单独断言

剩下 9 个用例因为 kui 的 1000ms 映射超时会把输出串到下一个用例的抓取窗口里，
所以只参与全序列比对。

**有一处故意没有复刻上游的测试 bug**：`basic.exp` 的
"parent map deleted, child map still matches" 发送 `unmap_example1_subset`
并期望输出 `b`，但没有任何映射能产生 `b`，C driver 实际是原样回显。
那个 dejagnu 用例之所以「通过」，是因为前一个用例超时后在 expect 缓冲区里
留下了 `b\r\n(kui) `，被下一个正则匹配到了 —— 空过。
这里断言的是真实行为。详见 `test_kui.py` 的模块 docstring。

### 调试钩子

两个环境变量，出问题时很有用：

```bash
CGDBPY_KEYLOG=E:\path\keys.txt   # 每个到达 handler 的按键（带时间戳）
CGDBPY_MILOG=E:\path\mi.txt      # 每一行收发到的 MI 记录（带时间戳）
```

上面第 5、6、8 条坑都是靠这两个日志定位的。

## 与 C 版的差异

有意为之，不是遗漏：

| | C 版（Linux） | C 版（Windows） | Python 版 |
|---|---|---|---|
| GDB 通道 | console pty + `new-ui mi` 双通道 | MI 单通道管道 | MI 单通道管道 |
| GDB 窗 | libvterm 终端模拟 | 合成哑终端 + 手写行编辑器（405 行） | 合成行模型 + prompt_toolkit Buffer |
| 行编辑 | gdb 自己的 readline | 手写（无 Tab 补全、无 Ctrl-R） | prompt_toolkit 原生（历史、词操作、kill ring） |
| 语法高亮 | 8 个 flex 词法器 | 同左 | Pygments（语言覆盖广得多） |
| 按键映射 | lib/kui | 同左 | `cgdbpy/kui.py`，198 项行为对齐 |
| inferior 全屏程序 | 支持（pty） | 不支持（管道） | 不支持（管道） |
| 启动 | ~8.5 ms | ~8.5 ms | ~260 ms |
| 分发 | 单文件 | 6.6 MB 全静态单文件 | 需要解释器或打包 |

**尚未实现**（C 版有）：文件选择对话框（`o` 键，目前提示用 `:open`）、
反汇编窗口、`:set syntax` 的语言强制、多源码缓冲切换、鼠标。

## 已知限制

1. **启动约 260ms**（解释器 31ms + import prompt_toolkit 131ms + 首帧 98ms），
   C 版是 8.5ms。消不掉，只能权衡。
2. **ESC 有约 100ms 延迟**，这是 VT 输入模式下区分 lone ESC 和转义序列的
   固有代价，C 版的 `ttimeoutlen` 默认值也是 100ms。
3. **inferior 全屏程序不可用**（tty 是管道不是终端），行交互正常。
   和 C 版 Windows 移植的限制一致。
4. **GDB 窗没有 Tab 补全**。可以用
   `-interpreter-exec console "complete ..."` 实现，prompt_toolkit 的
   `Completer` 接口天生适配 —— 这是 Python 版可以超过 C 版 Windows 移植的地方，
   但还没做。
5. **`:map` 只作用于源码窗**。GDB 窗焦点下按键归 prompt_toolkit 的 Buffer，
   用户映射不参与（C 版用 `imap` 覆盖这个场景）。
