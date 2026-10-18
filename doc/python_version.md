# Python 版 CGDB 可行性分析

> 结论基于 2026-10-19 在本机（Windows 10 / Python 3.11.9 / GDB 17.1 mingw32）的实测。
> 所有带 **[实测]** 标记的数字都可以用附录 A 的脚本复现；带 **[估算]** 的是推断，
> 没有跑过。基准脚本在 `.tmp/pyver/`。

## 0. 结论先行

**可行，而且比 C 版的 Windows 移植更容易。** 三个理由：

1. **架构难题已经被 Windows 移植解掉了。** 原版 cgdb 依赖
   `gdb --nw -ex "new-ui mi /dev/pts/N"` 开出**第二条 MI 通道**（`fork_util.cpp:226`），
   主控制台仍走 pty + gdb 自己的 readline，其输出字节流（含 readline 的光标转义序列）
   由 libvterm 终端模拟器还原成屏幕。`windows` 分支已经证明：
   `new-ui` 在原生 Windows GDB 上根本不可用（`doc/porting-feasibility.md` §3.2），
   改用 **`gdb --interpreter=mi` 单通道走管道 + 合成 GDB 窗口** 后全功能可用
   （`doc/win_specs.md` §7、§8，E2E 23/23）。**Python 版可以直接继承这个架构，
   一步跳过 pty、双通道和终端模拟器三座大山。**

   > 注意：`roadmap.txt` 里说「tgdb is still only capable of speaking the
   > annotation protocol」是**历史文档，已不成立**。实测全仓库
   > （排除 `win32/generated/`）只有 `gdbwire.c:7952` 和 `gdbwire.h:3181`
   > 两处注释提到 annotate，且都是在描述*别的*前端的用法。
   > **当前 cgdb 走的是 MI，不是 annotate。**

2. **Python 生态正好覆盖了 cgdb 最重的几块。** 全仓库手写 C/C++ 共 **41,255 行**
   （另有 26,209 行 flex 生成物在 `win32/generated/`，1,277 行 `.lpp` 词法源），
   其中约 **24,700 行是在重新发明 Python 已有的轮子**：
   `gdbwire`(11,409 行) → `pygdbmi` 或 400 行自写；`libvterm`(6,504 行) → 不需要；
   `lib/tokenizer`(459 行 + 1,277 行 flex) → `Pygments`；
   `lib/rline`(171 行) → `prompt_toolkit` 原生；
   `lib/util`(4,845 行平台层) → 标准库。

3. **性能不是障碍 [实测]。** MI 往返中位数 117 µs，Python 侧同步开销仅 30 µs；
   MI 大负载摄入 ~2 MB/s；prompt_toolkit 全屏 diff 渲染 1.98 ms/帧、73.5 fps。
   唯一真实的性能坑是 Pygments 全文 lex（28 µs/行），但有明确解法（§6.1）。

**代价**：启动从 8.5 ms 变成 ~260 ms（§5.4），分发从 6.6 MB 单文件变成
需要解释器或 20 MB+ 的打包产物，以及 32 位 mingw 那套约束不再适用
（Python 版天然跨位数，反而更好）。

**TUI 选型推荐 `prompt_toolkit`**，理由和实测对比见 §4。

---

## 1. 先厘清：要做的是哪一个「Python 版 cgdb」

这个问题不先回答，后面全是空谈。有三条本质不同的路线：

### 路线 A：独立 TUI 进程 + GDB/MI 管道（**推荐**）

```
┌─ Python TUI (prompt_toolkit) ──────────────┐
│  源码窗 / GDB 窗 / 状态条 / : 命令行         │
│  vim 风格 keymap 引擎（kui 的等价物）        │
└──────────────┬─────────────────────────────┘
               │ stdin/stdout 管道，MI 记录流
┌──────────────▼─────────────────────────────┐
│ gdb --nw -q --interpreter=mi                │
│  用户命令 → -interpreter-exec console "..."  │
│  inferior → -gdb-set inferior-tty <pipe>    │
└─────────────────────────────────────────────┘
```

这就是 `windows` 分支 C 版的架构，也是 gdbgui 的架构
（[gdbgui 用 pygdbmi 管理 gdb 子进程并解析输出](https://www.gdbgui.com/howitworks/)）。
**能完整复现 cgdb 的交互模型**：上下分屏、ESC 切焦点、GDB 窗独立滚动、
vim 按键映射、`:set` 配置。

### 路线 B：gdb 内嵌 Python 脚本（gdb-dashboard 模式）

[gdb-dashboard](https://github.com/cyrus-and/gdb-dashboard) 是一个 `.gdbinit` 文件，
用 **gdb 自带的 Python API** 在每次停住时打印一个仪表盘。

- **优点**：工作量极小（一个文件），不需要任何协议解析 —— `gdb.selected_frame()`、
  `gdb.breakpoints`、`gdb.parse_and_eval()` 都是活的 Python 对象。
- **致命缺点**：**gdb 自己的 readline 拥有终端**。你只能在 stop 事件里打印，
  无法实现 cgdb 的核心体验 —— 下半屏是一个可以独立滚动、可以 vim 式操作的
  GDB 会话窗，上半屏源码窗有自己的焦点和断点操作。
  路线 B 做出来的是「更好看的 gdb-dashboard」，不是 cgdb。

**结论：路线 B 不满足需求，但值得作为「低成本副产品」考虑** —— 如果只想要
源码+局部变量+栈的自动展示，它一天就能做完。

### 路线 C：直接用 gdb 自带的 TUI

`gdb -tui`（Ctrl-X A）已经有分屏。但它的源码窗不能 vim 式操作、没有断点可视化、
没有文件对话框、不能配置 —— 这正是 cgdb 存在的理由。不算候选。

**下面全部按路线 A 展开。**

---

## 2. 模块映射与工作量

LOC 统计口径：`git ls-files` 里的 `.c/.cpp/.h`，**不含** `win32/generated/`
的 26,209 行 flex 生成物。「代码行」= 去掉空行和纯注释行。

| C 模块 | 原始行 | 代码行 | Python 替代 | 预估代码行 | 说明 |
|---|---:|---:|---|---:|---|
| `lib/tgdb/gdbwire.c+h` | 11,409 | 5,486 | **`pygdbmi`** 或自写 ~400 行 MI 解析 | 0–400 | 见 §3.1，这是最大的一笔节省 |
| `lib/tgdb/tgdb.cpp+h` | 2,852 | 1,665 | MI 客户端 + 命令队列 + 回调 | ~800 | 命令映射表（`reference.md` §5.2）可直接照搬 |
| `lib/vterm/` | 6,504 | 4,910 | **不需要** | 0 | MI 模式下 gdb 输出是纯文本，无转义序列 |
| `lib/tokenizer/` + `.lpp` | 459+1,277 | 294 | **Pygments** | ~120 | 只需写 token→颜色组 的映射表 |
| `lib/kui/` | 2,627 | 1,364 | prompt_toolkit KeyProcessor + 自定义 map 层 | ~500 | 见 §3.2，**不能直接用 ptk 的 vim 模式** |
| `lib/rline/` | 171 | 85 | prompt_toolkit 原生 | 0 | rline 本来就是把 readline 键表导出给 kui |
| `lib/util/` | 4,845 | 2,921 | 标准库（`asyncio`/`subprocess`/`os`） | ~200 | pty/termios/select 全部消失 |
| `cgdb/` UI 层 | 11,985 | 7,286 | prompt_toolkit Layout + 自写窗格 | ~2,500 | 见 §3.3 |
| **合计** | **40,852** | **24,011** | | **~4,500** | **约 5.3 倍缩减 [估算]** |

（仓库手写 C/C++ 总计 41,255 原始行 / 24,291 代码行；上表少的 403 / 280 行是
`lib/tgdb/driver.cpp` + `test.cpp` 两个测试 driver，Python 版会换成 pytest，不计入。）

**[估算] 工作量**：一个熟悉 prompt_toolkit 的人，做到「日常可用」
（源码窗 + GDB 窗 + 断点 + 单步 + 搜索 + `:set`）约 **3–5 周**；
做到 C 版 Windows 移植的功能对等（含文件对话框、语法高亮全语言、
scrollback、增量搜索高亮）约 **2–3 个月**。
对照：C 版 Windows 移植走了 M0–M6 七个里程碑。

缩减的主要来源不是「Python 更简洁」，而是 **三块重量级 C 代码在 Python 里根本不用写**：
gdbwire（有 pygdbmi）、vterm（架构上不需要）、平台层（标准库）。

---

## 3. 三个技术要点

### 3.1 GDB 协议层：pygdbmi 还是自写？

`gdbwire.c` 8,193 行是**外部项目 GDBWIRE 1.0 的 amalgamation**
（`gdbwire.h` 头部注明：Robert Rossi，git rev `6faba36`），不是 cgdb 自己写的。
它是一个**纯 MI 解析器**（`gdbwire_mi_*` 系列类型），把 MI 记录流切成
result / stream / async / prompt 四类并回调。Python 版需要的正是同一件事，
而这件事在 Python 里只要几百行。

我实测了一个 **~60 行的 MI 记录分类器**（`.tmp/pyver/bench_mi.py`）：

```
classify() 成本: 1.08 µs/行  =>  925,412 行/秒   [实测]
```

MI 是行式协议，四类记录（`^result` / `~@&stream` / `*+=async` / `(gdb)` prompt）
用四个正则就能切开。**gdb 根本产不出 92 万行/秒，所以解析永远不是瓶颈。**

- **用 pygdbmi**：省事，且被 gdbgui 长期验证。但它把记录解析成嵌套 dict，
  对 `~"..."` 流记录里的 C 转义处理有自己的约定，且是同步阻塞式 API
  （`GdbController.get_gdb_response()`），要接进 asyncio 得自己包一层。
- **自写**：~400 行可以做到完全异步 + token 化请求/响应配对，
  和 `tgdb.cpp` 的命令队列语义一一对应。

**建议自写**，理由是 §3.1.1 那个坑。

#### 3.1.1 [实测] 命令/响应配对必须用 token，不能用 prompt

第一版基准用「等 `(gdb)` 提示符」来判断命令完成，结果：

```
200x -break-info（等 prompt）: 中位数 15,926 µs   ← 离谱
```

改用 MI token（发 `123-break-info`，等 `123^done`）后：

```
gdb+pipe（发出 → 读到 ^done）: 中位数 117 µs  p95 12,127 µs  max 24,474 µs
Python 唤醒（读到 → 主线程恢复）: 中位数  30 µs  p95     50 µs  max    358 µs
                                                          [实测]
```

**15.9 ms 是同步方式的假象，不是 gdb 慢。** 顺带排除了一个常见怀疑：
Windows 的 15.6 ms 定时器粒度与此无关（实测 `Event.wait` 1ms 请求 → 1.43 ms 返回）。

两个可直接用于设计的结论：
- **Python 侧开销只有 30 µs**，比 gdb 自身的 117 µs 小一个量级。协议层不值得优化。
- **p95 12 ms / max 24 ms 的长尾在 gdb 侧**，C 版一样有。不要试图在 Python 里「修」它。

### 3.2 kui 不能靠 prompt_toolkit 的 vim 模式顶替

这是最容易低估的一块。cgdb 的 `lib/kui` 是**两层前缀树 + 双超时**
（`reference.md` §6）：

- `terminal_keys` 层：把 `ESC[A` 这类转义序列折叠成 `CGDB_KEY_*` 单值，
  超时 = `keycode_timeout`
- `normal_keys` 层：执行用户的 `:map` / `:imap`，超时 = `mapping_timeout`

prompt_toolkit 的 vim 模式是**固定的 vim 键位实现**，不是「可配置的映射引擎」。
cgdb 要的是用户能 `:map gg up`、`:map abc def`，且 `ab` 要在超时后原样吐出。
ptk 的 `KeyProcessor` 提供了正确的底层机制（前缀匹配 + timeout），
但映射表语义要自己实现。

**好消息**：`testsuite/kui.base/basic.exp` 是一份 **198 项的行为规格**
（`make check` 基线，见 AGENTS.md），覆盖了 `<S-a>`/`<C-a>`/`<Esc>`/`<Up>` 全套宏、
递归映射、子集/超集映射、unmap、部分匹配超时。**直接翻译成 pytest 就是
Python 版 kui 的验收标准**，不用自己发明测试。这是本仓库现成的、
最被低估的一笔资产。

### 3.3 UI 层：cgdb 本来就没有窗口管理器

`roadmap.txt` 里上游作者自己写着：

> CGDB essentially has no window manager. A window manager is the main feature
> that CGDB requires in order to better be able to easily display more
> information to the user.

cgdb 的 UI 就是：**两个可滚动文本窗格 + 一条状态行 + 一个 `:` 命令提示 +
一个文件对话框**。没有布局引擎、没有嵌套分割、没有鼠标。

这正好是 prompt_toolkit 的甜区，也正好是 Textual 的 widget/CSS 机制**用不上**的地方。
选 Textual 等于为了一个不需要的布局引擎付出 2 倍的渲染成本和一套异步消息泵的复杂度。

---

## 4. TUI 选型（实测对比）

同一负载、同一 ConPTY（40 行 × 110 列）、同一台机器、同一 Python 3.11.9。
布局仿 cgdb：上半屏源码窗、下半屏 GDB 控制台窗。
脚本：`.tmp/pyver/bench_app_textual.py`、`bench_app_ptk.py`，
用 `conpty_bench.py` 在**真实 ConPTY** 里跑（不是 headless mock，
和 C 版 E2E 用的是同一套 ConPTY 接线）。

| 指标 | prompt_toolkit 3.0.52 | Textual 8.2.3 | 备注 |
|---|---:|---:|---|
| 首次绘制 | 99.6 ms | 98.4 ms | 打平，都是 import 之后的时间 |
| **强制全屏渲染一帧** | **1.98 ms** (p95 2.71) | — | ptk 可直接调 renderer；Textual 无同步渲染 API |
| **持续绘制帧率** | **73.5 fps** (147 帧/2s) | **36.0 fps** (72 帧/2s) | ptk 快 2.0 倍 |
| 提交更新速率 | 33,548 /s | 5,165 /s | ptk 的 `invalidate()` 更轻 |
| 合并比（提交:绘制） | 456 : 1 | 143 : 1 | 两者都会合并，ptk 合并得更狠 |
| 每帧 VT 字节 | 2.7 KB | 5.3 KB | ptk 的 diff 更紧 |
| 单帧滚动延迟（中位） | 0.040 ms | 0.151 ms | |
| 灌入 5000 行控制台输出 | 0.20 ms 提交 | 636 ms 提交 | **不公平对比**，见下 |
| 灌入后实际重绘次数 | — | **2 帧** | Textual 的 RichLog 合并得非常好 |

**关于「灌入 5000 行」那一行的诚实说明**：ptk 侧我是一次性
`buffer.text = "\n".join(lines)`（0.2 ms），Textual 侧是 5000 次
`RichLog.write()`（每次投递一条消息，636 ms）。**这是 API 形状差异，不是框架性能差异** ——
Textual 有批量的 `write_lines()`，用它数字会好看得多。
但值得注意的是 Textual 在 5000 次写入期间**只重绘了 2 帧**，
说明它的合并机制在突发输出下表现很好，这一点对 debugger 恰恰重要。

### 4.1 推荐：prompt_toolkit

**理由（按权重）**：

1. **架构契合**。cgdb 的灵魂是 kui 那套「可配置 vim 映射 + 原始字节透传给 gdb」。
   ptk 的 `KeyProcessor` 就是前缀树 + 超时，和 kui 同构；
   Textual 的按键是 action/command 模型，要在上面实现 `:map abc def` 得逆着框架走。
2. **渲染更快、输出更省 [实测]**。2 倍帧率，一半的 VT 字节。
   对 GDB 突发输出（`info functions .` 一次 593 KB）是实打实的余量。
3. **同域先例**。IPython / ptpython / pgcli / mycli 都是
   「全屏 + vim 键位 + 补全 + 流式输出」的 ptk 应用，这条路被走通过很多次。
4. **同步/异步都行**。GDB 输出突发时，ptk 可以同步处理完再渲染；
   Textual 强制 asyncio，且**消息泵是个陷阱**（§4.2）。
5. **依赖更轻 [实测]**。`prompt_toolkit 3.0.52` 的依赖列表就一项：`wcwidth`。
   `textual 8.2.3` 则要拖进 `markdown-it-py[linkify]`、`mdit-py-plugins`、
   `platformdirs`、`pygments`、`rich`、`typing-extensions`。
6. **Textual 的语法高亮不覆盖 C/C++ [实测]**。`textual[syntax]` extra 声明的
   tree-sitter grammar 是：bash、css、go、html、java、javascript、json、markdown、
   python、regex、rust、sql、toml、xml、yaml —— **没有 c，也没有 cpp**。
   对一个主要用来调 C/C++ 的调试器前端，这意味着要么自己额外装
   `tree-sitter-c`/`tree-sitter-cpp` 并手动注册 grammar，
   要么放弃 `TextArea` 的增量高亮改走 Pygments（那就等于自己实现 §6.1 的缓存方案，
   Textual 在这块的优势就没了）。

**代价**：没有 widget 工具箱，文件对话框、状态条、滚动条都要自己写。
但如 §3.3 所说，cgdb 的 UI 本来就极简，这部分工作量比听起来小。

### 4.2 [实测] Textual 的消息泵陷阱

第一次跑 Textual 基准，3.7 秒里只输出了 **410 字节** VT —— 一帧内容都没画。
dump 出来看，全是进/出备用屏的序列：

```
b'\x1b[?1015h\x1b[?1004h\x1b[>1u\x1b[?2004h\x1b[2J\x1b[m\x1b[H' + 40×'\r\n'
... 3.4 秒空白 ...
b'\x1b[?2004l...\x1b[?25h'      ← 直接 teardown
```

根因：我把基准写成了 `async def on_mount`。**Textual 在消息泵内部 await
消息处理器**，所以一个长时间运行的 `on_mount` 会把整个泵堵死，
compositor 永远拿不到渲染消息 —— 即使中间 `await asyncio.sleep(0)` 也没用，
因为被堵住的是泵而不是事件循环。

改成 `def on_mount` + `create_task()` 后立刻正常（96 KB VT，36 fps）。

这对 debugger 是**结构性风险**，不是基准脚本的小毛病：cgdb 必须在
GDB 吐 593 KB 输出、inferior 疯狂打印、用户连续按键时保持响应。
Textual 模型下所有这类工作都必须正确切分到 worker 里，一处写错就是整个 UI 冻住
—— 而且**冻住时不报错、不崩溃，只是不重绘**，极难调试。
ptk 的同步渲染模型没有这个失效模式。

### 4.3 其他候选

- **urwid**：成熟、list-based widget、事件循环无关。但 API 老化、社区活跃度低、
  没有 vim 键位机制，相对 ptk 没有优势。本机未安装，未实测。
- **py_cui / asciimatics / blessed**：更底层或更小众，都要自己写更多。
- **curses（windows-curses）**：能最直译 C 版（`sys_win.cpp` 的 `swin_*` 抽象层
  几乎可以一对一映射），但 Windows 上的 curses 是二等公民
  （C 版正是因为这个才选 PDCurses-wincon），且拿不到 ptk 的输入处理。**不推荐。**
- **Rich**：不是 TUI 框架，是渲染库。可以作为 ptk 的补充做富文本，
  但单独用它做不了全屏交互应用。

---

## 5. 实测数据汇总

### 5.1 GDB/MI 通道（`gdb.exe` 17.1 mingw32，管道）

| 项 | 数值 |
|---|---:|
| 启动到首个 prompt | 81.8 ms |
| `-break-info` 往返（token 同步，200 次） | 中位 **117 µs**，p95 12.1 ms，max 24.5 ms |
| 其中 Python 侧唤醒开销 | 中位 **30 µs**，p95 50 µs |
| `-file-list-exec-source-files` | 89.2 ms |
| 200× `-break-insert` | 124.7 ms（623 µs/条） |
| `run` → `*stopped`（断点命中） | 85.5 ms |
| `continue` → `*stopped`（退出） | 30.2 ms |
| MI 记录分类器成本 | 1.08 µs/行 → **925,412 行/秒** |

### 5.2 大负载摄入

| 命令 | 负载 | 耗时 | 吞吐 |
|---|---:|---:|---:|
| `info functions .` | 593.5 KiB / 15,094 行 | 296.9 ms | **2.0 MB/s，50,837 行/s** |
| `disassemble main` | 1.5 KiB / 34 行 | 1.1 ms | 1.4 MB/s |

**2 MB/s 的摄入能力对交互式调试足够**：593 KB 的极端输出 300 ms 内吃完，
其中大部分是 gdb 自己在生成。

### 5.3 Pygments 语法高亮

对本仓库真实源文件（Pygments 2.20.0）：

| 文件 | 行数 | 全文 lex | µs/行 | 行/秒 |
|---|---:|---:|---:|---:|
| `lib/tgdb/gdbwire.c` | 8,194 | 177.8 ms | 21.7 | 46,096 |
| `lib/vterm/state.c` | 2,266 | 93.0 ms | 41.0 | 24,376 |
| `cgdb/interface.cpp` | 1,866 | 50.6 ms | 27.1 | 36,869 |
| **6 个文件合计** | **17,266** | **484.5 ms** | **28.1** | **35,635** |

- 只 lex 可见窗口：40 行 **0.216 ms**（4,624 次全重绘/秒），100 行 1.168 ms
- `style_for_token()`：1.64 M tokens/s，可忽略

### 5.4 启动延迟（这是 Python 版最明显的退步）

| | 中位数 |
|---|---:|
| C 版 `cgdb.exe --version`（6.6 MB 全静态） | **8.5 ms** |
| `python -c pass` | 31.0 ms |
| `python -c "import prompt_toolkit.application"` | **162 ms** |
| `import textual.app` | 190.6 ms |
| `import pygments` | 0.6 ms（惰性，几乎免费） |

**用户感知的总启动 ≈ 162 ms（解释器+import）+ 99 ms（首帧）≈ 260 ms [估算]**，
对比 C 版的近乎瞬时。约 **20–30 倍慢，但绝对值仍可接受**。
可用 `zipimport` 预热、延迟 import（Pygments 已经是惰性的）、
或 PyInstaller onefile（会更慢，因为要解压）来调整，但**消不掉**。

---

## 6. 硬约束与风险

### 6.1 [实测] 不能只 lex 可见窗口 —— 会给出错误颜色

直觉方案是「滚动时只 lex 屏幕上那 40 行」，0.216 ms，看起来完美。**但它是错的。**

对 `lib/tgdb/tgdb.cpp`（2,184 行）在 11 个偏移处各取 40 行窗口，
把「窗口独立 lex」的结果和「全文 lex」的结果逐行对比：

```
 offset  错行数   首个分歧
    300       3   line 305: 'static void tgdb_win_prompt(struct tgdb *tgd'
    500       1   line 510: 'static bool win_esc_step(struct tgdb *tgdb)'
    700       2   line 723: 'static void tgdb_commands_send_source_files('
    900       1   line 917: 'source_position_changed(struct tgdb *tgdb, g'
   1300      36   line 1300: ''          ← 40 行里错 36 行
   1700      35   line 1700: ''          ← 40 行里错 35 行

6/11 个窗口与全文 lex 结果不一致
```

1300 和 1700 那两个窗口起点落在 `/**` 块注释内部，独立 lex 会把整个窗口当代码染色。
本文件最长跨行 token 是 19 行，但**这是文件相关的** ——
一个含大段块注释或 C++ raw string 的文件可以跨几千行。

**必须采用的方案**（三选一，按推荐排序）：

1. **全文 lex 一次 + 按行缓存 token 列表**，滚动时只查缓存。
   代价：8,194 行文件 178 ms（可接受，且可在后台线程做 + 渐进显示）；
   10 万行文件 ~2.8 s（**必须**后台线程 + 先显示无高亮文本再回填）。
2. **安全回退点启发式**：从窗口起点向上扫，找到「不可能处于多行构造中」的行
   （C 里：第 0 列且不在注释/字符串内），从那里开始 lex。省时间但需要按语言写规则，
   且规则写错就是静默的染色错误。
3. 用 tree-sitter 增量解析（Textual 的 `TextArea` 走这条路）。
   正确且增量，但要为每种语言装 grammar 包
   （本机 `tree_sitter` 核心已装，`tree_sitter_c`/`tree_sitter_cpp` **未装**）。

C 版没有这个问题 —— flex 词法器足够快，直接全文 lex。

### 6.2 inferior 全屏程序

MI 模式下 inferior 的 tty 是管道不是终端。行交互（`scanf`/`printf`）正常，
**全屏交互（vim 类）不可用** —— 这和 C 版 Windows 移植的已知限制 #7 完全一样
（`doc/win_specs.md` §11）。

要支持就得引入终端模拟器（`pyte`，本机未装）+ ConPTY，
等于把 C 版故意绕开的 `libvterm` 又请回来。**建议 v1 不做**，
和 C 版保持同样的限制。

### 6.3 合成 GDB 窗口的功能缺口

C 版 Windows 移植的已知限制 #2：**无 Tab 补全、无 Ctrl-R 历史搜索**，
因为 gdb 的 readline 根本没在跑。Python 版继承同一缺口。

但 **Python 版反而更容易补上**：`-interpreter-exec console "complete <line> <col>"`
能拿到补全候选，而 prompt_toolkit 的 `Completer` 接口天生就是干这个的。
C 版要自己写补全 UI，ptk 只要实现一个 `get_completions()`。
**这是 Python 版可以超过 C 版 Windows 移植的第一个点。**

同理，ptk 的 `Buffer` 自带完整行编辑（历史、搜索、词操作），
而 C 版是在 `tgdb.cpp:241-645` 手写了 **405 行（319 代码行）** 的字节级行编辑器
—— 20 个 `win_edit_*` 函数，逐个实现插入/退格/删除/Home/End/左右/
kill-line/kill-to-eol/kill-word-back/历史翻阅/ESC 序列状态机
（`win_specs.md` §8.2）。**Python 版这块几乎免费。**

### 6.4 分发

| 方式 | 体积 [估算] | 启动 | 备注 |
|---|---:|---|---|
| 要求用户装 Python + `pip install` | ~0 | 260 ms | 对开发者友好，对普通用户不友好 |
| `pipx install` | ~0 | 260 ms | 隔离环境，仍需系统有 Python |
| PyInstaller onefile | 20–30 MB | **更慢**（要解压到临时目录） | 单文件但启动退步 |
| PyInstaller onedir | 25–40 MB | ~260 ms | 启动最好的打包方案 |
| Nuitka | 15–25 MB | 略快 | 编译成 C，构建慢 |

对比 C 版：**6.6 MB 单文件全静态，8.5 ms 启动**。
**分发是 Python 版最难看的一项**，没有好的解法，只能权衡。

### 6.5 其他

- **32 位约束消失**：C 版受 mingw32 限制（32 位 gdb 只能调 32 位 inferior，
  `win_specs.md` §11.3）。Python 版只是发 MI 命令给 gdb 进程，
  **gdb 是几位的和前端无关**，天然跨位数。这是 Python 版的一个实质优势。
- **CJK 输入**：C 版限制 #4（VT 输入模式按控制台码页转换，非 UTF-8 码页下 CJK 键入会乱）。
  ptk 在 Windows 上走 `ReadConsoleInputW` 宽字符路径，**大概率不受影响，但未实测**。
- **`pygdbmi` 维护状态 [实测]**：PyPI 最新是 `0.11.0.0`，上传时间
  **2023-01-29**，至今（2026-10）近三年没有新版本；`requires_dist` 为空
  （零依赖，这点是好的）。这也是 §3.1 建议自写解析器的理由之一 ——
  MI 协议稳定，400 行自己维护比依赖一个半停滞的库更可控。

---

## 7. 可以直接复用的资产

这是「Python 版比 C 移植更容易」的核心论据 —— 不是 Python 更好写，
而是**这个仓库里已经躺着大量可直接搬的东西**：

| 资产 | 位置 | 怎么用 |
|---|---|---|
| MI-over-pipes 传输设计 | `doc/win_specs.md` §7 | 管道接线、两个致命陷阱、EOF 判定，全部照搬 |
| 合成 GDB 窗口规格 | `doc/win_specs.md` §8 | 行模型的字节语义（`\n`/`\r`/`\b`/TAB/折行/UTF-8 不切断）是一份现成的行为规范 |
| 内嵌行编辑器键位表 | `doc/win_specs.md` §8.2 | ESC 序列三种变体、词操作、Ctrl-A/E/B/F/K/U/W/D/L —— ptk 大多原生支持 |
| MI 命令映射表 | `doc/reference.md` §5.2 | `tgdb_get_gdb_command` 的完整映射，直接翻译成 dict |
| 四条核心数据流 | `doc/reference.md` §1.4 | 输入/控制台输出/MI 输出/resize 的完整链路，是 Python 版事件循环的设计图 |
| **kui 的 198 项行为规格** | `testsuite/kui.base/basic.exp` | **翻译成 pytest 就是 keymap 引擎的验收标准** |
| E2E 场景矩阵 | `doc/win_specs.md` §10.2 + `.tmp/e2e_cgdb.py` | 23 项场景 + ConPTY 驱动 + VT 屏幕解析断言，换个被测程序就能跑 Python 版 |
| ConPTY 接线配方 | `doc/porting-feasibility.md` §3.1 | 三个坑（属性表传句柄值、USESTDHANDLES+NULL、ClosePseudoConsole 阻塞） |
| `:set` 选项清单 | `cgdb/cgdbrc.cpp` + `doc/cgdb.texi` | 配置项语义的权威定义 |

**特别注意 `.tmp/e2e_cgdb.py` + `.tmp/conpty_run.py`**：
这是一套已经调通的「在 ConPTY 里跑真程序 + VT 屏幕解析 + 断言」的测试框架。
Python 版可以**用同一套框架测自己**，验收标准和 C 版完全对齐。
（本文档 §4 的 TUI 基准就是复用这套 ConPTY 接线跑的。）

---

## 8. 建议的推进路径

如果决定做，按这个顺序，每阶段都有可验证的产出：

| 阶段 | 目标 | 验收 |
|---|---|---|
| **P0** | MI 客户端：token 化请求/响应、异步读线程、四类记录分类、命令队列 | 复刻 `.tmp/mi_driver.cpp` 的 11 项（basic 7 + interrupt 4） |
| **P1** | 无 UI 的 CLI 原型：跑起 inferior、断点、单步、`*stopped` 定位 | 能对一个真实程序完成 break/run/next/step/print 全流程 |
| **P2** | ptk 双窗格骨架 + 源码窗（无高亮）+ 焦点切换 | 上下分屏、ESC 切焦点、源码窗滚动 |
| **P3** | kui 等价物：两层前缀树 + 双超时 + `:map`/`:imap` | **198 项 pytest 全绿**（从 `basic.exp` 翻译） |
| **P4** | 合成 GDB 窗：行模型 + 行编辑 + 历史 | 复刻 `win_specs.md` §8.1 的字节语义表 |
| **P5** | Pygments 高亮 + 按行缓存（§6.1 方案 1） | 大文件后台 lex 不卡 UI |
| **P6** | 断点可视化、搜索、`:set`、文件对话框 | 对齐 C 版 E2E 23 项场景矩阵 |
| **P7** | 打包分发（§6.4） | onedir 产物在干净机器上可跑 |

**P0 和 P3 是两个真正的风险点**，其余都是体力活。
建议先只做 P0+P1（约 3–5 天），拿一个能跑的 CLI 原型验证 MI 层，
再决定要不要往下走 —— 这也符合本仓库一贯的「先做开工实验再动手」的做法
（`win_specs.md` §4 的 M0 就是 8 个实验）。

---

## 9. 什么情况下**不该**做 Python 版

诚实地列出反面理由：

1. **如果目标只是「Windows 上能用 cgdb」—— 已经达成了。**
   `windows` 分支的 C 版是全功能原生版，6.6 MB 单文件，8.5 ms 启动，
   E2E 23/23 + MI 11/11 + Linux 回归 198/198 全绿。
   Python 版在**每一个性能指标上都更差**，且分发更麻烦。
2. **如果目标是给上游 cgdb 贡献**，上游是 C11/C++11 项目，
   Python 重写不可能被合并，等于开一个独立的新项目，要自己长期维护。
3. **如果目标是「更快的开发速度来试新想法」**，那 Python 版有价值 ——
   但更省事的做法是给 C 版加一个 Python 插件接口，或者直接用路线 B
   （gdb 内嵌 Python）做原型。

**Python 版真正站得住的理由只有两个**：
- 想要一个**易于二次开发/扩展**的 cgdb（写个新窗格、新命令，Python 比 C 快一个量级）
- 想要**超过 C 版 Windows 移植的功能**（Tab 补全、Ctrl-R 搜索、鼠标、
  更丰富的语法高亮），而这些在 C 里做代价很高（§6.3）

如果这两条不成立，建议不做。

---

## 附录 A：基准复现

脚本都在 `.tmp/pyver/`（`.gitignore` 的 `/.*/*` 已覆盖，不入库）：

| 脚本 | 测什么 |
|---|---|
| `bench_mi.py` | MI 往返延迟（prompt 同步，**有缺陷**，保留作为 §3.1.1 的反面证据） |
| `bench_mi2.py` | token 同步版 + 大负载尝试 |
| `bench_mi3.py` | **延迟分解**：gdb+pipe vs Python 唤醒；Windows 定时器粒度排查 |
| `bench_bulk.py` | 大负载摄入吞吐（`info functions .` = 593 KB） |
| `bench_pygments.py` | Pygments 全文/窗口 lex 吞吐 + style 查表成本 |
| `bench_window_lex.py` | **窗口 lex 正确性**：11 个偏移 × 40 行 vs 全文 lex 逐行对比 |
| `conpty_bench.py` | ConPTY 基准harness（复用 `.tmp/conpty_run.py` 的接线配方） |
| `bench_app_textual.py` | Textual 渲染基准（含 driver 层帧计数） |
| `bench_app_ptk.py` | prompt_toolkit 渲染基准（含 renderer 层帧计数） |
| `target.c` / `target.exe` | 被调试的测试程序（`int big[100000]` + `compute()`） |

跑法（Git Bash，仓库根目录）：

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"        # gdb.exe 和被调试程序需要
cd .tmp/pyver
PY=/c/Share/Python311/python.exe

$PY bench_mi3.py                              # 延迟分解
$PY bench_bulk.py                             # 大负载吞吐
$PY bench_pygments.py                         # 高亮吞吐
$PY bench_window_lex.py                       # 窗口 lex 正确性
$PY conpty_bench.py bench_app_ptk.py     5000 out_ptk.json
$PY conpty_bench.py bench_app_textual.py 5000 out_textual.json
BENCH_CAPTURE=vt.bin $PY conpty_bench.py bench_app_textual.py 500 out_t.json  # dump VT
```

注意：
- **不要用 `D:\Dev\mingw32\bin\python.exe`**（那是 mingw32 自带的 3.14.3），
  本文所有数字都用 `C:\Share\Python311\python.exe`（3.11.9）。
- ConPTY 尺寸由 `BENCH_ROWS` / `BENCH_COLS` 控制，默认 40×110，
  和 C 版 E2E 一致。
- `bench_mi.py` 的 prompt 同步方式会给出 ~100 倍偏高的延迟，**别拿它的数字做决策**
  （这正是 §3.1.1 要说明的坑）。

## 附录 B：本机 Python 生态现状

| 包 | 状态 | 用途 |
|---|---|---|
| `prompt_toolkit` 3.0.52 | ✅ 已装 | **推荐的 TUI 框架** |
| `textual` 8.2.3 | ✅ 已装 | 备选 TUI（含 `TextArea`） |
| `rich` 14.3.3 | ✅ 已装 | 富文本渲染（Textual 的依赖，也可单独用） |
| `Pygments` 2.20.0 | ✅ 已装 | 语法高亮，**替代 `lib/tokenizer`** |
| `tree_sitter` | ✅ 已装（核心） | Textual `TextArea` 增量高亮所需 |
| `tree_sitter_c` / `_cpp` | ❌ 未装 | **Textual 的 `syntax` extra 也不含这两个**（§4.1 第 6 点），要 C/C++ 增量高亮必须自己装并注册 |
| `pygdbmi` | ❌ 未装 | MI 解析（§3.1 建议自写，可不装） |
| `pyte` | ❌ 未装 | 终端模拟，仅 §6.2 全屏 inferior 才需要 |
| `urwid` | ❌ 未装 | 备选 TUI，未实测 |
| `pexpect` | ❌ 未装 | 路线 A 用不到（直接 `subprocess` + 管道） |
| `pywinpty` | ❌ 未装 | 仅 ConPTY 路线需要；本文的 ConPTY 接线是手写 ctypes，无依赖 |

Python 3.11.9 @ `C:\Share\Python311\python.exe`。
另有 mingw32 自带的 Python 3.14.3 @ `D:\Dev\mingw32\bin\python.exe`（**未用于本文任何测量**）。

## 附录 C：参考

- [gdbgui — How it Works](https://www.gdbgui.com/howitworks/)：
  路线 A 的先例，明确说明用 pygdbmi 管理 gdb 子进程并解析输出
- [cs01/pygdbmi](https://github.com/cs01/pygdbmi)：MI 解析库
- [cyrus-and/gdb-dashboard](https://github.com/cyrus-and/gdb-dashboard)：
  路线 B 的先例，gdb 内嵌 Python API
- [GDB Front Ends（GDB Wiki）](https://sourceware.org/gdb/wiki/GDB%20Front%20Ends)：
  现有前端全景，含 gdb 自带 TUI
- [Textualize/textual](https://github.com/textualize/textual)：Textual 上游
- 本仓库：`doc/reference.md`（架构）、`doc/win_specs.md`（Windows 实施规格）、
  `doc/porting-feasibility.md`（可行性实测）
