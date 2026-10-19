# CGDB Python 版实施规格与设计依据

> **本文的读者是后续要改这份代码的 coding agent（或人）。**
> 目标是让你在不重踩已知坑的前提下安全地迭代。
>
> 全文分两部分：
>
> - **第一部分（§0–§13）实施规格** —— 现在是什么、不能碰什么、缺什么从哪接。
>   逐模块规格、不变量、陷阱与对应回归测试、未实现项的接入点、验证流程。
>   **日常改代码只需要这一部分。**
> - **第二部分（§14–§19 + 附录）设计依据与决策记录** —— 为什么是这个样子。
>   可行性论证、被否决的路线、TUI 选型实测对比、模块映射与工作量、
>   分发与平台约束、以及「什么情况下不该做这件事」。
>   **要做架构级改动、或想推翻某个既有决策前先读这一部分。**
>
> 其他文档：
> - `python/README.md` —— **用户视角**：怎么跑、按键表、`:命令`、已知限制。
> - `doc/reference.md` —— C 版架构（数据流、模块原理），Python 版的行为基准。
> - `doc/win_specs.md` —— C 版 Windows 移植规格。Python 版的传输层直接继承它，
>   §7（MI-over-pipes）和 §8（合成 GDB 窗口）是本实现的上游设计。
> - `doc/porting-feasibility.md` —— Windows 移植可行性实测（ConPTY 接线配方等）。
>
> 行号引用基于 commit `189bf65`。改动后行号会漂，**以符号名为准，行号只作定位提示**。
> 带 **[实测]** 的数字都可以用附录 A 的脚本复现；带 **[估算]** 的是推断，没跑过。

---

# 第一部分：实施规格

## 0. 30 秒上手

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"          # gdb.exe / gcc.exe
cd python
python -m pytest tests/ -q                       # 714 项，约 2.5 分钟
python cgdb.py -d D:\Dev\mingw32\bin\gdb.exe --source cgdbpy/mi.py -- -q 某个32位exe
```

依赖：Python 3.11+、`prompt_toolkit`、`pygments`、`wcwidth`（ptk 自带依赖）。
集成测试和 E2E 需要 `gdb` + `gcc`，缺了会 **skip 而不是 fail**。

**改完必须跑的两条**（顺序有意义）：

```bash
python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/test_gdb_integration.py   # 2 秒，先跑
python -m pytest tests/ -q                                                              # 2.5 分钟，提交前
```

只跑第一条是不够的 —— 见 §9.1，本项目**四个最严重的 bug 全部只有 E2E 能抓到**。

---

## 1. 验收基线（当前状态）

| 项 | 数值 |
|---|---|
| 测试总数 | **714 全绿**（从推送后的全新克隆复跑验证过） |
| 代码量 | `cgdbpy/` 3,576 行 + `cgdb.py` 203 行；测试 4,980 行 |
| 启动到首帧 | ~260 ms（解释器 31 + import ptk 131 + 首帧 98） |
| ESC 键延迟 | ~107 ms（受 `ttimeoutlen=100` 支配，见 §7.3；嫌慢可 `:set ttm=50`） |
| 普通键延迟 | 0–4 ms |
| MI 往返 | 中位 117 µs（gdb 侧）+ 30 µs（Python 侧） |
| MI 摄入吞吐 | ~2 MB/s / ~50k 行/s |

测试分布：

| 文件 | 数量 | 覆盖 | 需要外部依赖 |
|---|---:|---|---|
| `tests/test_kui.py` | 359 | 按键映射引擎 + 与 C `kui_driver` 黄金输出交叉验证 | 无 |
| `tests/test_app.py` | 82 | UI 层逻辑，用 `FakeGdb` | 无 |
| `tests/test_mi.py` | 72 | MI 记录解析 | 无 |
| `tests/test_console.py` | 54 | GDB 窗行模型字节语义 | 无 |
| `tests/test_cgdbrc.py` | 54 | `:set` 选项，与 `cgdb/cgdbrc.cpp` 交叉比对 | 无（交叉比对需 C 源码在位，否则 skip） |
| `tests/test_source.py` | 46 | 源码加载/编码/高亮缓存/断点表 | 无（窗口 lex 反证需 C 源码在位） |
| `tests/test_gdb_integration.py` | 25 | **真实 gdb** 子进程 | gdb + gcc |
| `tests/e2e/test_e2e.py` | 22 | **真实 cgdb.py + 真实 gdb 跑在真实 ConPTY** | Windows + gdb + gcc |

---

## 2. 架构

### 2.1 进程与通道

```
┌─ python cgdb.py ────────────────────────────────────────────────┐
│  prompt_toolkit Application（主线程 / asyncio）                  │
│    源码窗 FormattedTextControl   GDB 窗 FormattedTextControl     │
│    状态条                        编辑行 BufferControl            │
│                    ↑ invalidate()                                │
│  CgdbApp（app.py）── 回调 ──────────────────────────┐            │
└──────────┬──────────────────────────────────────────┼───────────┘
           │ stdin/stdout 匿名管道                     │
           │                                          │
┌──────────▼──────────────────────────┐   ┌───────────▼──────────┐
│ gdb --nw -q                         │   │ inferior tty         │
│   -ex "set pagination off"          │   │  Win: 命名管道(重叠IO)│
│   -ex "set confirm off"             │   │  POSIX: pty          │
│   --interpreter=mi                  │   │                      │
│   <用户参数>                         │   │  ← 被调试程序的 stdout│
└──────────┬──────────────────────────┘   └──────────────────────┘
           │ -gdb-set inferior-tty <未加引号的设备名>
           └──────────────────────────────────────┘
```

**单通道 MI**，不是 C 版 Linux 的 `new-ui mi` 双通道 —— 那个在原生 Windows GDB
上不可用（`doc/porting-feasibility.md` §3.2 有实测证据）。用户敲的 gdb 命令一律走
`-interpreter-exec console "..."`，回复以 MI 记录形式回来再路由。

### 2.2 线程模型 ⚠️ 最重要的不变量

**六个执行体，职责严格分离。搞混会死锁 —— 而且已经死锁过一次（§7.1）。**

| 线程 | 名字 | 职责 | 绝对不可以 |
|---|---|---|---|
| 主线程 | asyncio loop | ptk 渲染、按键处理、`dispatch()` | 阻塞等 gdb（会冻住整个 UI） |
| 读线程 | `gdb-mi-reader` | `read()` gdb stdout → `LineSplitter` → `parse_line` → **`_complete()` 配对请求** → 入队 `_events` | 调用用户回调 |
| 派发线程 | `gdb-mi-dispatch` | 出队 → `_dispatch()` → 触发用户回调 | 无限制（可以阻塞调 `call()`） |
| 泵线程 | `gdb-mi-pump` | 从 `_queue` 取命令写入 gdb stdin，**一次只允许一条在飞** | 轮询（见 §7.4） |
| inferior 读线程 | `inferior-pipe` / `inferior-pty` | 读被调试程序输出 → `_on_inferior_output` | — |
| 临时 worker | 无名 daemon | `_run_async()` / `send_console_command()` 派生，执行阻塞的 `call()` | 长期存活 |

**为什么配对必须留在读线程**：`call()` 阻塞在 `req.done.wait()`，而 `done.set()`
由 `_complete()` 触发。如果 `_complete()` 和回调在同一线程，那么「回调里调 `call()`」
就是自己等自己 —— 这正是 `_on_stopped` → `refresh_breakpoints()` → `-break-info`
曾经冻死整个会话的原因。

**推论（改代码时照此判断）**：
- 回调里**可以**安全调用 `client.call()`（阻塞）。回归测试：
  `test_gdb_integration.py::test_callback_may_issue_a_blocking_call`
- 主线程（ptk 事件循环）里**不可以**调用 `call()`。所有会阻塞的 gdb 交互必须经
  `_run_async()`（`app.py:1053`）或 `send_console_command()`（`app.py:1023`）派生 worker。
- 回调里改 UI 状态后调 `self.invalidate()`（线程安全），**不要**直接调 ptk 的渲染 API。

### 2.3 四条数据流

对应 C 版 `doc/reference.md` §1.4，形状一致但传输不同。

**A. 用户按键**
```
ConPTY → ptk Win32Input(VT 模式) → Vt100Parser → KeyPress
 → 键绑定（app.py:275 _build_keybindings）
 → on_key（app.py:340）→ translate_key（app.py:107）→ cgdb_key int
 → _key_queue → _drain_keys（app.py:353）
 → kui KeyContext.getkey()（用户映射层，可能 PENDING）
 → dispatch（app.py:386）→ 按焦点分发
     ├─ FOCUS_SOURCE  → source_key（app.py:437）
     ├─ FOCUS_GDB     → gdb_key（app.py:548）；普通编辑归 ptk Buffer
     └─ FOCUS_COMMAND → cmd_buffer 的 accept_handler
```

**B. GDB 控制台输出**
```
读线程 → parse_line → ~ 记录 → 入队
派发线程 → _dispatch → on_console（app.py:953）
 → ConsoleModel.add_bytes（console.py:71）
 → invalidate() → 下一帧 _console_text（app.py:864）
```

**C. inferior 输出**
```
inferior 读线程 → _on_inferior_output（app.py:963）→ ConsoleModel.add_bytes
```
注意：inferior 输出**不经 gdb**，直接从我们给 gdb 的那个 tty 设备读回来。

**D. 停止事件**
```
读线程 → *stopped → 入队
派发线程 → on_stopped（app.py:976）
 → 解析 frame.fullname / frame.line → self.exec_line
 → goto_file_line（app.py:1174）→ 必要时 open_file
 → refresh_breakpoints（app.py:1106，阻塞 call，合法）
 → invalidate()
```

---

## 3. 逐模块规格

### 3.1 `cgdbpy/mi.py`（412 行）—— MI 记录解析

C 版对应物：`lib/tgdb/gdbwire.c`（8,193 行，vendored 的 GDBWIRE 1.0 amalgamation）。

**公开 API**

| 符号 | 行 | 说明 |
|---|---:|---|
| `unescape_c_string(s)` | 42 | 解 MI c-string 转义，**含八进制 `\ddd`** |
| `escape_c_string(s)` | 99 | 反向；与上面构成往返 |
| `quote(s)` | 128 | 包成完整的 MI c-string 参数 |
| `breakpoint_location(path, line)` | 133 | ⚠️ 见 §7.2，别自己拼 |
| `parse_line(line)` | 329 | 一行 → `Record`；**永不抛异常** |
| `parse_value(text, pos)` | 281 | 独立解析一个 MI value |
| `LineSplitter` | 380 | 字节流 → 完整行，跨 chunk 保存残行与残 UTF-8 |
| `Record` | 304 | `kind`/`token`/`cls`/`payload`/`stream_kind`/`async_kind`/`raw` |

**常量**：`RESULT`/`STREAM`/`ASYNC`/`PROMPT`/`PARSE_ERROR`；
流类型 `STREAM_CONSOLE="~"` / `STREAM_TARGET="@"` / `STREAM_LOG="&"`；
异步类型 `ASYNC_EXEC="*"` / `ASYNC_STATUS="+"` / `ASYNC_NOTIFY="="`。

**不变量**

1. **`parse_line` 绝不抛异常。** 解析不了的行降级成 `PARSE_ERROR` 记录并保留 `raw`。
   理由：C 版的 `gdbwire_parse_error_callback` 也只记日志。一个畸形 gdb 构建
   不应该杀掉前端。测试：`test_mi.py::test_malformed_lines_do_not_raise`（8 种畸形输入）。
2. **MI 的 `const` 永远是带引号的 c-string。** 裸数字（`[[1,2]]`）不是合法 MI，
   必须降级为 `PARSE_ERROR` 而不是半解析。测试：`test_mi.py::test_bare_numbers_are_not_valid_mi`。
   → 不要为了"容错"去支持裸数字，那会掩盖真正的协议错误。
3. **八进制转义必须支持。** `\033` 是 gdb 传递 ESC 的方式。把它读成 `NUL + "33"`
   会毁掉每一条经过 `~` 记录的终端转义序列。
   测试：`test_mi.py::test_octal_escapes`（9 个参数化用例）+
   `test_octal_escape_survives_a_stream_record`。
4. **`LineSplitter` 必须能处理任意切分点**，包括切在多字节 UTF-8 序列中间。
   测试：`test_mi.py::test_splitter_utf8_split_across_chunks`、
   `test_splitter_byte_at_a_time`、`test_splitter_large_payload`（15,000 行）。
5. **result-list 的重复键折叠成 list。** `-break-info` 的
   `body=[bkpt={...},bkpt={...}]` 是「结果列表」而非「值列表」，两个 `bkpt`
   键要合并成 `[...]`。单行时是 dict —— 这个不对称是最经典的 MI 陷阱，
   由 `source.Breakpoints` 统一吸收，**别在调用点各自处理**。

**性能事实**：`parse_line` 约 1.08 µs/行 → 925k 行/s。gdb 产不出这个量级，
**解析永远不是瓶颈，不要优化它**。

### 3.2 `cgdbpy/gdbclient.py`（387 行）—— gdb 子进程与请求队列

C 版对应物：`lib/tgdb/tgdb.cpp` 的传输与命令队列部分。

**公开 API**

| 符号 | 行 | 说明 |
|---|---:|---|
| `GdbClient(...)` | 81 | `gdb_path` / `args` / `callbacks` / `cwd` / `env` |
| `.start()` | 120 | 拉起 gdb；**失败即抛 `GdbError`**，不静默降级 |
| `.stop(timeout)` | 161 | 关 stdin → wait → kill |
| `.call(cmd, timeout)` | 219 | 阻塞；`^error` **抛 `GdbError`** |
| `.send(cmd)` | 209 | 入队，不等结果 |
| `.console_command(text)` | 228 | `-interpreter-exec console <quoted>` |
| `.interrupt()` | 182 | Win: `GenerateConsoleCtrlEvent(CTRL_BREAK, pid)`；POSIX: SIGINT |
| `.running` | 178 | 属性 |
| `.stats` | — | `bytes`/`lines`/`records`/`parse_errors` 计数 |

**回调**（全部在派发线程上触发）：
`on_console(~)`、`on_target(@)`、`on_log(&)`、`on_stopped(*stopped)`、
`on_running(*running)`、`on_notify(cls, payload)`、`on_exit(code)`。

**不变量**

1. **token 配对，绝不等 `(gdb)` 提示符。** 实测：等提示符会得到 15.9 ms/命令的
   假延迟，token 配对是 117 µs —— 差 100 倍。
   见 §13.2 的实测分解。
2. **一次只允许一条命令在飞**（`_pump_loop`，`gdbclient.py:239`）。
   `_inflight` 非空时队列不推进。这复现了 C 版
   `is_gdb_ready_for_next_command` 的语义。
3. **`^error` 双重上报，各管一段**：`_dispatch` 负责把它回显给用户
   （`gdb: <msg>`），`call()` 负责让程序化调用方无法忽略它（抛异常）。
   → `_console_command_worker`（`app.py:1036`）因此**只**在超时/管道断裂时补报，
   否则用户会看到同一个错误两遍。改这里前先读那段注释。
4. **`start()` 必须 fail-fast 且错误可读。** C 版有过「gdb 不在 PATH 时静默崩溃」
   的 bug（commit `2c8c8ca`）。测试：
   `test_gdb_integration.py::test_missing_gdb_fails_fast_and_visibly`。
5. **`_fire()` 吞掉回调异常并打到 stderr。** 一个有 bug 的 UI 回调不能杀掉读线程。
   → 调试时如果发现"什么都没发生"，**先看 stderr**。
6. **`stop()` 之后 `_pump_loop` 要把残留请求全部置错并 `done.set()`**，
   否则等待方永久挂起。

### 3.3 `cgdbpy/inferior_tty.py`（276 行）—— 被调试程序的终端

C 版对应物：`lib/util/process_win.cpp` 的 `win_inferior_tty_*` / `lib/util/pseudo.cpp`。

| 类 | 行 | 平台 |
|---|---:|---|
| `InferiorTty` | 21 | 抽象基类 |
| `PtyInferiorTty` | 52 | POSIX（`pty.openpty()`，`-inferior-tty-set`） |
| `PipeInferiorTty` | 167 | Windows（命名管道，`-gdb-set inferior-tty`） |
| `create(on_output)` | 272 | 平台分发 |

**不变量**

1. ⚠️ **`gdb_argument()` 返回的字符串必须以「不加引号」的形式插进 MI 命令行。**
   这是本项目最阴的坑：加引号后 gdb 回 `^done`（**假装成功**），
   实际没打开设备，inferior 的 stdout 直接灌进 MI 管道变成 `PARSE_ERROR`。
   实测四种写法：不加引号的反斜杠 / 不加引号的正斜杠 → 都能用；
   加引号的三种（原始反斜杠、正斜杠、`mi.quote()`）→ 全废。
   基类 docstring（`inferior_tty.py:27`）记录了完整实测结论。
   回归测试：`test_gdb_integration.py::test_inferior_tty_must_not_be_quoted`
   —— 它**同时断言不加引号能用、加引号仍然是坏的**，这样将来 gdb 修好了
   这个测试会失败并提示你可以去掉 workaround。
2. **Windows 侧必须用重叠 I/O。** 同步命名管道上读线程常驻 `ReadFile` 时，
   主线程对同一句柄 `WriteFile` 会死锁（C 版 `win_specs.md` §7.2 实测）。
   读、写各自带 `OVERLAPPED` + 独立 event。
3. **`app.start_gdb()` 会校验 `-inferior-tty-show` 的返回值**（`app.py:906`）。
   因为 `^done` 不可信（见第 1 条），必须回读确认。
   → 如果你要改 tty 接线，这个校验是你的安全网，别删。
4. 没有 inferior tty 会怎样：MI 流被污染。这是 C 版 M0 阶段的实验 E2a
   （`win_specs.md` §4）确认过的，不是推测。
   测试：`test_gdb_integration.py::test_inferior_output_does_not_corrupt_the_mi_stream`
   断言 `stats["parse_errors"] == 0` 且 inferior 输出确实到达。

### 3.4 `cgdbpy/kui.py`（407 行）+ `keycodes.py`（326 行）—— 按键映射引擎

C 版对应物：`lib/kui/`（2,627 行）+ `kui_term.cpp` 的三张静态表。

| 符号 | 文件:行 | C 版对应 |
|---|---|---|
| `KeyTree` | kui.py:38 | `kui_tree.cpp` |
| `MapSet` | kui.py:147 | `kui_map_set.cpp` |
| `KeyContext` | kui.py:211 | `kui_ctx.cpp` |
| `KeyManager` | kui.py:305 | `kui_manager.cpp` |
| `terminal_map_set()` | kui.py:192 | `kui_term` 的 hard_coded_bindings + terminfo |
| `string_to_keys()` | keycodes.py | `kui_term_string_to_key_array` |
| `keys_to_bytes()` | keycodes.py | `kui_term_get_ascii_char_sequence_from_key` |
| `keys_to_display()` | keycodes.py | `kui_driver` 的输出格式 |
| `KEYCODES` / `HARD_CODED_BINDINGS` / `TERM_SEQUENCE_DEFAULTS` | keycodes.py | `cgdb_keycodes`(92 项) / `hard_coded_bindings`(47 项) / terminfo |

**匹配算法不变量**（照抄 `doc/reference.md` §6.2，改之前先读那节）

1. **最长匹配**：节点既有 `value` 又有 children 时，记住命中但**继续匹配**。
   `map abc` 与 `map abcde` 并存时敲 `abcd` 停手 → 生效 `abc`。
2. **`finalize()` 在「超时」和「失配」两种情况下都要提升中途命中。**
   只处理超时是 bug：`\033o` 必须产出 `ESC` 然后 `o`，而不是吞掉 ESC。
   我第一版就漏了失配分支。测试：
   `test_kui.py::test_esc_followed_by_unknown_char_emits_both`。
3. **提升中途命中后，多消费的键必须退回缓冲。** `found_len` 就是为此存在的。
   `partial_abcde` → `mmmde`（不是 `mmm`，也不是 `mmmd`）。
   期望值直接来自 `basic.exp:346-352`，别凭直觉改。
4. **递归展开**：命中的替换结果压回缓冲**前端**并重新参与匹配。
   `map first second` + `map second third` → `first` 产出 `third`。
5. **剪枝删除**：`remove()` 只物理删除「既无 value 又无 children」的节点。
   删父映射不能伤到子映射，反之亦然。两个方向都有测试。
6. **`defer_on_empty`**：输入耗尽且仍在 MATCHING 时返回 `PENDING` 而不是立即
   finalize。这是与事件循环集成的关键 —— 否则 `map abc` 在敲下 `a` 的瞬间
   就被拆散了。`flush()` 用于超时到点时强制解决。
   测试：`test_kui.py::test_pending_defers_instead_of_finalizing`。

**记法解析不变量**（`string_to_keys`）

- `<...>` 查找**大小写不敏感**（C 版用 `strcasecmp`），**首个匹配胜出**。
  所以 `<Del>` → `CGDB_KEY_DC`（10009）而不是字节 127 —— 表里 `<Del>` 出现两次。
- 未知记法 `<NoSuchKey>` **降级为字面字符**，不报错。
- 宏内部再遇 `<` → 把已积累的字面量吐出去，从新 `<` 重新开始。
- 未闭合的 `<` → 全部按字面输出。
- `<CR>` / `<Return>` / `<Enter>` 都是 `CTRL_M`。

**终端序列表**：`TERM_SEQUENCE_DEFAULTS` 里的 PageUp/F1-F12 等值
**已逐字节对齐真实 C `kui_driver` 在 `TERM=xterm-256color` 下的输出**
（黄金参考里 `CGDB_KEY_PPAGE[27][91][53][126]` 等）。改这些值会让
`test_kui.py` 的逐用例交叉验证失败 —— 那是有意的。

**MSDOS 方向键的旋转**：`\033[0A`=UP 但 `\033[0B`=**LEFT**、`\033[0C`=RIGHT、
`\033[0D`=**DOWN**。不是笔误，C 版 `hard_coded_bindings` 就是这样。

### 3.5 `cgdbpy/console.py`（251 行）—— GDB 窗行模型

C 版对应物：`cgdb/scroller_win.cpp`。字节语义规格在 `win_specs.md` §8.1。

| 符号 | 行 |
|---|---:|
| `ConsoleModel` | 39 |
| `.add_bytes(data)` | 71 |
| `.add_text(text)` | 100 |
| `.visible_lines(height, delta)` | 183 |
| `.max_delta(height)` | 198 |
| `ScrollState` | 209 |

**字节语义**（逐条都有测试，`test_console.py`）

| 输入 | 行为 |
|---|---|
| `\n` | 提交当前行 |
| `\r` | 光标列归零（**后续字符覆写，不是追加**） |
| `\b` | 光标列 -1，下限 0。**只移动不删除** —— 删除是行编辑器的事 |
| TAB | 前进到下一个 8 的倍数，用空格填充 |
| 可打印 / UTF-8 | 在光标处覆写并前进 |
| 行满 | 折行 |
| BEL 及其他 C0 控制符 | **忽略**（不推进光标） |

**与 C 版的一处有意差异**：C 版按**字节**计列，本实现按**字符**计列，
所以多字节字符是原子的、永不被切开。这严格强于 C 版的规则
（"永不切开 UTF-8 序列——续字节允许溢出"），且让模型与源编码无关。
`console.py` 的模块 docstring 记录了这一点。

**`add_bytes` 的 UTF-8 暂留**：跨调用切分的多字节序列会被扣在 `_pending` 里，
凑齐才输出。`flush_pending()` 用于强制吐出（会产生替换字符，但不丢数据）。
测试：`test_console.py::test_utf8_split_across_add_bytes_calls`。

**`visible_lines` 的底部对齐**：返回**恰好** `height` 行，内容不足时在**顶部**
补空行。这是有意的 —— 见 §7.5，ptk 从顶部开始显示 formatted text。

**`resize()` 不重排已提交行**：与 C 版 v1 的简化一致
（`win_specs.md` §8.1："已提交行保持旧折行"）。
测试：`test_console.py::test_resize_keeps_committed_wrapping`。

### 3.6 `cgdbpy/source.py`（361 行）—— 源码缓冲、高亮、断点表

| 符号 | 行 | 说明 |
|---|---:|---|
| `SourceFile` | 30 | 加载 + 编码探测 + 高亮缓存 |
| `.highlight_sync()` | 99 | 全文 lex，按行缓存 token |
| `.highlight_async(on_done)` | 120 | 后台线程版本 |
| `.ensure_highlighted()` | 141 | 小文件同步、大文件异步 |
| `.tokens_for_line(i)` | 151 | 未就绪时返回 `None` |
| `Breakpoints` | 159 | 断点表 |
| `.update_from_mi(payload)` | 165 | 从 `-break-info` 结果全量替换 |
| `.absorb_async(cls, payload)` | 207 | 从 `=breakpoint-*` 通知增量更新 |
| `.absorb_result_bkpt(payload)` | 251 | 从 `^done,bkpt={}` 结果吸收 |
| `.at(path, line)` | 280 | 路径大小写/形式不敏感 |
| `SourceView` | 294 | 窗口状态：top/cursor/marks/search |
| `ASYNC_LEX_THRESHOLD` | 27 | 4000 行 |

**⚠️ 高亮策略是被实测逼出来的，不要"优化"成窗口化 lex。**

只 lex 可见窗口对 C/C++ 是**错的**：窗口起点落在 `/* ... */` 或 raw string 内部时，
整屏染色全错。实测 `lib/tgdb/tgdb.cpp`（2,184 行）上 11 个 40 行窗口，
**6 个与全文 lex 不一致**，其中两个 40 行里错 35–36 行。
本文件最长跨行 token 是 19 行，但这是文件相关的 —— 大段块注释或 C++ raw string
可以跨几千行。

三个测试守着这条：
- `test_source.py::test_cached_highlighting_matches_a_full_file_lex`
  —— 我们采用的策略必须与独立的全文 lex 逐行一致
- `test_source.py::test_windowed_lexing_is_demonstrably_wrong`
  —— **反证**朴素方案不可接受。如果它哪天不再复现，说明缓存策略的理由需要重新审视
- `test_source.py::test_a_window_starting_inside_a_block_comment_is_the_failure_mode`
  —— 钉住具体机制

代价：8,194 行文件全文 lex 178 ms（可接受）；10 万行约 2.8 s
（**必须**走 `highlight_async` + 先显示无高亮文本再回填）。
`ASYNC_LEX_THRESHOLD = 4000` 就是这个权衡的开关（实测 28 µs/行，4000 行 ≈ 0.11 s）。

**编码探测顺序**（`SourceFile._read`，`source.py:54`）：
`utf-8-sig` → `utf-8` → locale 首选编码 → `latin-1` 兜底。
**永不抛异常、永不丢字节。** CRLF 会被规范化掉（`self.crlf` 记录原状），
因为 `testsuite/win32/cases_files` 那类 CRLF+CJK 源文件是真实场景。

**断点表的三个入口对应 gdb 的三种不对称行为**（§7.6 有实测依据）：

| gdb 行为 | 用哪个入口 |
|---|---|
| `-break-info` 的 `BreakpointTable` | `update_from_mi()` |
| `-break-insert` 的 `^done,bkpt={}`（**无通知**） | `absorb_result_bkpt()` |
| 控制台 `break` 触发的 `=breakpoint-created` | `absorb_async()` |
| `=breakpoint-deleted`，载荷是 **`id=`** 不是 `bkpt` | `absorb_async()` |
| 多位置断点 `1.1` / `1.2` | 三者都按 `.` 前缀归并到父编号 |

### 3.7 `cgdbpy/cgdbrc.py`（222 行）—— `:set` 配置

C 版对应物：`cgdb/cgdbrc.cpp`。23 个选项的名称、缩写、类型、默认值
**逐项照抄** `cgdbrc_init_config_variables()` 与 `cgdbrc_init_config_options()`。

**防漂移机制**：`test_cgdbrc.py::test_every_c_option_name_is_present`
**用正则直接从 `cgdb/cgdbrc.cpp` 里抓出 `ConfigVariable("name","abbrev",CONFIG_TYPE_*)`
三元组**，逐个比对名称、缩写、类型。C 版加了新选项而这个测试失败，就是该同步了。
（C 源码不在位时 skip。）

**查找规则**：全名 → 缩写 → **唯一前缀**（vim 风格）。大小写不敏感。

**`:set` 语法**：
- `:set name=value`
- `:set name` —— bool/int 置 1，string 返回当前值
- `:set all` / `:set` —— 返回全部选项的字符串列表
- 有 `choices` 的 string 选项会校验取值（如 `winsplit` 只接受
  top/bottom/left/right/even）
- `on_change` 钩子仅在值**真的变化**时触发

**rc 文件**：`cgdb.py:_load_rc()` 依次读 `~/.cgdbrc` 和 `./.cgdbrc`。
`load_file()` **收集错误而不中断**，返回 `["path:lineno: msg", ...]`，
好文件里的坏行不影响其余行。

### 3.8 `cgdbpy/highlight.py`（128 行）—— Pygments → ptk 样式

| 符号 | 说明 |
|---|---|
| `style_for_token(tok)` | 沿 Pygments token 继承链向上找，返回 `class:xxx` |
| `COLOR_STYLE` / `MONO_STYLE` | 两套 `Style.from_dict` 字典 |
| `build_style(color=True)` | `:set color=off` 时切到 MONO |

`MONO_STYLE` 不是全空 —— 保留了结构性强调（`bold` / `reverse`），
这样关颜色后当前行、断点标记、状态条仍然可辨。

C 版对应物是 `cgdb/highlight_groups.cpp` 的 HLG_* 组。要加新颜色组，
在 `_DIRECT` 里加 token 映射 + 两个 STYLE 字典里各加一项即可。

### 3.9 `cgdbpy/app.py`（1,215 行）—— 界面与事件循环

C 版对应物：`cgdb/cgdb.cpp`（主循环）+ `cgdb/interface.cpp`（布局与焦点枢纽）。

**布局**（`_build_ui`，`app.py:207`）

```
HSplit([
  source_window    FormattedTextControl(_source_text)   D(weight=1, preferred=0.6)  focusable
  status_window    FormattedTextControl(_status_text)   height=1
  console_window   FormattedTextControl(_console_text)  D(weight=1, preferred=0.4)  focusable
  ConditionalContainer(edit_window,    filter=~in_command)   height=1  BufferControl(gdb_buffer)
  ConditionalContainer(cmdline_window, filter= in_command)   height=1  BufferControl(cmd_buffer)
])
```

用 `ConditionalContainer` 切换编辑行/命令行，**不要**去改 `HSplit.children` ——
那会让 ptk 的布局缓存失配。

**焦点**：`FOCUS_SOURCE` / `FOCUS_GDB` / `FOCUS_COMMAND`（`app.py:57-59`）。
`self.focus` 是逻辑焦点，`app.layout.focus(...)` 是 ptk 焦点，两者由
`set_focus()`（`app.py:770`）同步。

⚠️ **源码窗和控制台窗的 `FormattedTextControl` 必须 `focusable=True`**，
否则 `Layout.focus()` 会拒绝该窗口，ESC 静默失效（异常被 `set_focus` 的
try/except 吞掉，表现为"按了没反应"）。

**按键绑定的优先级设计**（`_build_keybindings`，`app.py:275`）

ptk 的合并顺序是「默认绑定 < 页面导航 < **app 级（我们的）** < 其他容器 < **当前焦点容器的绑定**」，
且 `matches[-1]` 胜出。所以：

- `Keys.Any` 带 `filter=~editing`，其中 `editing = _buffer_owns_keys()`
  （`app.py:321`）：GDB 编辑态（非滚动模式）或命令态时为 True。
  **这时普通字符归 ptk 的 Buffer**，我们完全不介入 —— 这正是免费拿到
  行编辑器（历史、词操作、kill ring）的原因。
- ESC / PageUp / PageDown / Ctrl-L / Ctrl-C / F1–F12 **无条件绑定**，
  任何焦点下都归 cgdb。
- 方向键 / Home / End 只在 `editing & scrolling` 时归我们
  （GDB 窗滚动模式下要能翻，其余时候要给 Buffer 翻历史）。

⚠️ **`Keys.Escape` 必须 `eager=True`**（`app.py:288` 附近）。
不加的话 ptk 认为它可能是更长序列的前缀，**永远不触发**。
实测：不加 eager 时 ESC 完全不到达 handler。

⚠️ **`event.key_sequence[-1]`，不是 `event.key_press`**（后者不存在）。
写错的表现是**每个按键都静默失败** —— 异常被 ptk 吞掉，界面毫无反应，
而单元测试发现不了（它们直接调 `on_key()` 传假对象）。
只有 E2E 能抓到。见 §9.1。

**kui 集成**：`self.mapctx` 是 `KeyContext(defer_on_empty=True)`，
读回调是 `_read_queued_key()`（从 `_key_queue` 取）。
`_drain_keys()`（`app.py:353`）循环取键直到 `None` 或 `PENDING`；
`PENDING` 时 `_arm_map_timeout()` 用 `loop.call_later(timeoutlen)` 排一个
`_map_timeout()`，到点 `flush()` 强制解决。

⚠️ **只用了 kui 的用户映射层（normal_keys），没用终端序列层（terminal_keys）。**
ptk 自己解码终端输入（Windows 上走 `ReadConsoleInputW`，比字节流解析更可靠）。
`terminal_map_set()` 仍然存在且被 359 项 kui 测试完整覆盖 —— 它是
`basic.exp` 那 198 项规格的一部分，也是 raw-byte 输入路径的实现。
**不要因为它"没被 app 用到"就删掉。**

**渲染契约**（`_source_text` `app.py:790` / `_console_text` `app.py:864`）

⚠️ **按实际可见高度生成行，不要按固定 budget 多生成再让 ptk 裁剪。**
ptk 从**顶部**开始显示 formatted text；在顶部补空行会把真实内容推出可视区
（表现为 GDB 窗一片空白）。需要底部对齐时，补齐**恰好** `height` 行。

高度来源：`_source_height()` / `_console_height()` 读上一帧的
`render_info.displayed_lines`（⚠️ 属性名不是 `visible_line_count`，那个不存在），
首帧无 `render_info` 时回退到 `view.height` / 估算值。
两个窗格的高度由布局权重决定而非内容决定，所以**不存在内容↔高度循环依赖**，
用上一帧的值是稳定的。

**gutter 格式**（源码窗每行前缀）：
`[B或空格][右对齐行号][=>或两空格][标记字符或空格]`
—— 断点标记、行号、执行箭头、`m<x>` 设置的标记。
`showmarks=off` 时最后一列恒为空格。

**命令执行**（`execute_command`，`app.py:618`）
- `/pat` `?pat` → `_do_search`
- `:cmd` → `_do_colon`（`app.py:643`）：`q`/`quit`/`q!`、`h`/`help`、`set`、
  `open <file>[:line]`、`map`/`imap`/`unmap`/`iunmap`、`clear`
- 其余 → `send_console_command`（当作 gdb 命令）

`:open` 的 `file:line` 解析用 `rpartition(":")` 且要求行号是纯数字，
否则整体当路径 —— 这样 Windows 的 `C:\...` 不会被误切。

---

## 4. 按键全链路（改按键行为前必读）

```
物理按键
 → ConPTY → conhost → INPUT_RECORD
 → ptk Win32Input（ENABLE_VIRTUAL_TERMINAL_INPUT）
 → Vt100Parser                     ← lone ESC 在这里被扣住，见 §7.3
 → KeyPress(key, data)
 → KeyProcessor._process            ← 绑定优先级与 eager 在这里生效，见 §3.9
 → 我们的 handler → on_key(app.py:340)
     ├─ CTRL_C → interrupt() 直接返回（不进 kui）
     └─ 其余 → translate_key → _key_queue
 → _drain_keys(app.py:353)
 → mapctx.getkey()                  ← 用户 :map 展开；可能返回 PENDING
     ├─ PENDING → _arm_map_timeout → timeoutlen 后 flush
     └─ int → dispatch(app.py:386)
         ├─ key == cgdbmodekey(默认 ESC) → 退出滚动模式 + 切到源码窗
         ├─ FOCUS_COMMAND → _command_key（实际是空操作，Buffer 自己处理）
         ├─ F1/F5/F6/F7/F8/F10/Ctrl-L → 全局动作
         ├─ FOCUS_SOURCE → source_key(app.py:437)
         └─ FOCUS_GDB    → gdb_key(app.py:548)
 → invalidate() → 下一帧重绘
```

**两键序列的实现方式**：`m<x>`（设标记）、`'<x>`（跳标记）、`gg`（回首行）
都靠 `self.last_key` 手工实现（`source_key` 开头），**不是**靠 kui 映射。

⚠️ 这是有意的：如果 `g` 单键就立即跳顶，那么 `:map gg <Up>` 永远不可能生效
（第一个 `g` 已经被消费掉了）。C 版同样用 `last_key_pressed`
（`interface.cpp:1425-1444`）。
测试：`test_app.py::test_lone_g_does_not_jump` +
`test_app.py::test_map_command_registers_a_mapping`（验证用户映射能**覆盖**内建 `gg`）。

---

## 5. 与 C 版的行为对齐点

改动这些行为前，先确认你是在**修 bug** 还是在**破坏对齐**。

| 行为 | 权威来源 |
|---|---|
| 功能键 F1/F5/F6/F7/F8/F10 的语义 | `interface.cpp:1511-1537` |
| ESC = cgdbmodekey = 切回源码窗；已在源码窗时无操作 | `interface.cpp:1546-1585` |
| **启动焦点 = GDB 窗**（一进来就能敲 gdb 命令；源码窗才是 vim 式按键） | `interface.cpp:139` |
| **gdb 就绪即打开主源文件**：启动时发 `-file-list-exec-source-file`（= C 的 tgdb_request_current_location），把调试对象的主文件带行号开进源码窗——不需要 run/break，也不需要 `--source` | `tgdb.cpp:1172-1177, 1982-1986` |
| **GDB 窗内 `:` 和 `/` 是普通字符，归 gdb**；进了源码窗才是 cgdb 的命令键 | `interface.cpp:996-1000` |
| **`:` / `/` 命令行画在中间状态栏行**（顶替状态栏那一行，不是屏幕底部） | `interface.cpp:459-472` |
| **gdb 退出（EOF）→ cgdb 跟着退出**；启动期就死则留在屏幕上展示报错 | `cgdb.cpp:760-772` |
| **`Ctrl-W`（源码窗）切换横向 / 纵向分屏**；纵向时源码+状态栏在左列、`\|` 分隔线一列、GDB 窗全高在右列，状态栏是左列最底行 | `interface.cpp:17-31, 1498-1509` |
| **`=` / `-` 逐行（横向）逐列（纵向）移动分割线；`+` / `_` 在 1/4 刻度间跳**；`window_shift` 两方向共用、切换时原值保留 | `interface.cpp:672-752, 1263-1281` |
| **窗口尺寸是每帧从实时终端尺寸推导的**（ptk Dimension callable），resize 由 ptk 的 0.5s 尺寸轮询触发重绘 | `interface.cpp` 的 HEIGHT/WIDTH 活值 + if_resize_term |
| **`:` 命令集 = C 的 commands[] + NUMBER 语法**：`:N`/`:+N`/`:-N` 跳行滚动、全部调试命令（`:r` `:start` `:c` `:n` `:nexti` `:s` `:stepi` `:f` `:u` `:up` `:down` `:k`）、`:insert` `:focus` `:e`；未知命令同样报错。`:open` `:clear` 为本版扩展 | `cgdbrc.cpp:98-141, 1058-1073` |
| **源码窗数字计数**：`5j`、`12G`（G_line_number 累积，非数字即清零） | `interface.cpp:1312-1317` |
| **源码窗 `u` = until 到光标行** | `interface.cpp:1304-1311` |
| **GDB 窗滚动模式键**：`Ctrl-U/D` 半页、`gg`（单 `g` 不动）、`F11/F12`、`q/i/Enter` 退出并停在底部 | `interface.cpp:908-975` |
| **GDB 窗内 `Ctrl-L` 归 readline，cgdb 不接管**（只有源码窗里才是重绘） | `interface.cpp:983-997` |
| **源码窗 `h`/`l` 横向滚动**；GDB 滚动模式 `h`/`l`/`0`/`$` 横向平移 | `interface.cpp:1243-1247, 955-962` |
| **GDB 滚动模式 `n`/`N`/`/`/`?` 控制台内搜索**（正反向、定位到可视区顶部） | `interface.cpp:968-980` |
| **`/` 输入时逐字符递进跳转**（部分正则编译失败则原地不动） | `status_bar_regex_input` |
| **`:syntax`/`:set syntax` 强制 lexer；`:shell` 挂起 UI 起子 shell** | `cgdbrc.cpp command_parse_syntax / command_do_shell` |
| `i` → GDB 窗，`s` → GDB 窗 + 滚动模式 | `interface.cpp:1443-1452` |
| 空格 = 切换断点，`t` = 临时断点 | `interface.cpp:1291-1300` |
| `o` = 文件对话框：**已实现**（`cgdbpy/filedlg.py`），全屏接管（if_draw 的 FILE_DLG 分支），j/k/翻页/h/l/`/`搜索/gg/数字+G，Enter 打开、q/ESC 取消；列表来自 `-file-list-exec-source-files` | `filedlg.cpp:584-673` |
| GDB 窗滚动语义、`[delta/max]` 状态覆盖层 | `win_specs.md` §8.1 |
| 合成行编辑器的键位表 | `win_specs.md` §8.2 |
| MI 命令映射表 | `reference.md` §5.2 |
| `:set` 选项全集与默认值 | `cgdb/cgdbrc.cpp` |
| 按键映射引擎语义 | `testsuite/kui.base/basic.exp`（198 项） |

**一处故意不对齐上游测试**：`basic.exp` 的
"parent map deleted, child map still matches" 发送 `unmap_example1_subset`
却期望输出 `b`，但没有任何映射能产生 `b`（C driver 实际原样回显）。
那个 dejagnu 用例"通过"是因为前一个用例超时后在 expect 缓冲区留下了
`b\r\n(kui) `，被下一个正则匹配到 —— **空过**。
本实现断言真实行为。详见 `test_kui.py` 的模块 docstring 与
`test_parent_unmapped_child_still_matches`。
**不要为了"和 dejagnu 一致"把它改回去。**

---

## 6. 配置项速查

23 项，全部与 C 版同名同缩写同默认值。常用的：

| 选项 | 缩写 | 类型 | 默认 | 影响 |
|---|---|---|---|---|
| `color` | `color` | bool | on | 切换 `COLOR_STYLE` / `MONO_STYLE` |
| `tabstop` | `ts` | int | 8 | 源码窗与控制台的 TAB 展开 |
| `scrollbackbuffersize` | `sbbs` | int | 10000 | `ConsoleModel` 上限 |
| `wrapscan` | `ws` | bool | on | 搜索是否回绕 |
| `ignorecase` | `ic` | bool | off | 搜索大小写 |
| `hlsearch` | `hls` | bool | off | ⚠️ **已读取但尚未实现高亮渲染** |
| `showmarks` | `showmarks` | bool | on | gutter 是否显示标记列 |
| `cgdbmodekey` | — | string | `esc` | 支持 `esc` / `tab` / `ctrl-g` |
| `timeoutlen` | `tm` | int | 1000 | kui 映射超时（ms） |
| `ttimeoutlen` | `ttm` | int | 100 | ⚠️ **直接决定 ESC 延迟**，见 §7.3 |
| `winminheight` | `wmh` | int | 0 | `=`/`-` 调整的下限（横向分屏） |
| `winminwidth` | `wmw` | int | 0 | `=`/`-` 调整的下限（纵向分屏） |
| `winsplit` | `winsplit` | string | `even` | 1/4 分割刻度，`src_big`/`src_full`/`gdb_big`/`gdb_full`（`top_*`/`bottom_*` 为兼容旧写法） |
| `winsplitorientation` | `wso` | string | `horizontal` | `horizontal` / `vertical`，即 Ctrl-W 切换的那个状态 |
| `syntax` | `syn` | string | 空 | ⚠️ **已存储但尚未用于强制 lexer** |
| `disasm` | `dis` | bool | off | ⚠️ **未实现**（无反汇编窗） |

标 ⚠️ 的三项是"配置项存在但功能未接线"，见 §8。

`_apply_config()`（`app.py:712`）是唯一的生效点：改样式、滚动上限、wrapscan、
`mapctx.timeout`、`app.ttimeoutlen`。**新增会产生副作用的选项时记得在这里挂上。**

---

## 7. 陷阱清单（每条都有回归测试）

这些全部是开发过程中实测踩到的，不是理论推演。

### 7.1 回调里阻塞 → 读线程死锁

**症状**：第一次命中断点后整个会话冻死，再无反应。
**定位手段**：`CGDBPY_MILOG` 显示流量停在 `6-break-info`，之后再无收发。
**根因**：回调跑在读线程上，`_on_stopped` → `refresh_breakpoints()` →
`call("-break-info")` 阻塞等结果记录，而结果记录只能由这个被阻塞的读线程送达。
**修法**：配对（`_complete`）留在读线程，用户回调移到独立派发线程。
**测试**：`test_gdb_integration.py::test_callback_may_issue_a_blocking_call`

### 7.2 两处 MI 参数拼写

| 场景 | 错 | 对 |
|---|---|---|
| `-gdb-set inferior-tty` | `-gdb-set inferior-tty "\\.\pipe\x"` | `-gdb-set inferior-tty \\.\pipe\x`（**不加引号**） |
| `-break-insert` 位置 | `-break-insert -- "C:\d\f.c":12` | `-break-insert -- "C:\d\f.c:12"`（**整体一个字符串**） |

第一条的错误表现是 `^done` + 静默失效（inferior 输出污染 MI 流）。
第二条的错误表现是 `^error,msg="Problem parsing arguments"`。

注意第二条与 C 版**不同**：C 版在 console 侧用 `break "file":N`
（`reference.md` §5.2），那条路走 gdb 自己的参数解析器，能work；
走 MI 就不行。

**测试**：`test_inferior_tty_must_not_be_quoted`、`test_breakpoint_location_quoting`、
`test_breakpoint_in_a_path_with_spaces`。
用 `mi.breakpoint_location()` 和 `InferiorTty.mi_command()`，别手拼。

### 7.3 ESC 延迟 500ms

**根因**：VT 输入模式下 ptk 的 `Vt100Parser` 会扣住 lone ESC 等后续字节，
只有 `Application.ttimeoutlen` 到点才 `flush_keys()`，而它的默认值是 **0.5 秒**。
**实测**：ESC 从发送到到达 handler 499 ms / 524 ms；普通键 0–3 ms。
**修法**：`_apply_ttimeoutlen()`（`app.py:259`）把 cgdb 自己的 `ttimeoutlen`
（默认 100 ms）接上去 → 降到 107 ms。
**这是固有代价**，不是 bug：区分 lone ESC 和转义序列必须等。C 版的
`ttimeoutlen` 默认值同样是 100 ms。
**测试**：`test_e2e.py::test_key_latency_is_not_the_500ms_escape_bug`
（断言 < 0.45 s，专门守这个回归）

### 7.4 泵线程轮询抢 GIL

`_pump_loop` 曾用 `time.sleep(0.001)` 轮询 —— 每秒 1000 次唤醒，
在 CPython 上与 UI 的 asyncio 循环抢 GIL。
改成 `threading.Event.wait(0.2)` + `send()` 时 `set()`。
（实测这一项**不是** ESC 延迟的主因，主因是 §7.3；但轮询本身仍然是错的。）

### 7.5 窗格内容顶部补空行 → 内容被推出可视区

**症状**：GDB 窗一片空白，只有最底下的 `(gdb)` 编辑行。
**根因**：ptk 从顶部开始显示 formatted text。按固定 budget（如 40 行）生成、
在顶部补齐，会让真实内容落在第 36–40 行，而窗格只有 19 行高。
**修法**：按实际可见高度生成；底部对齐时补齐**恰好** `height` 行。

### 7.6 gdb 断点通知的三种不对称

实测 gdb 17.1：

| 触发方式 | 通知 | 断点信息在哪 |
|---|---|---|
| `-break-insert` | **无** `=breakpoint-created` | `^done,bkpt={...}` |
| 控制台 `break X` | **有** `=breakpoint-created` | 通知的 `bkpt` + `~` 流记录 |
| 控制台 `delete N` | **有** `=breakpoint-deleted` | 载荷是 **`id="N"`**，没有 `bkpt` |

按 `bkpt` 取删除事件会**静默忽略所有删除**。
`-break-info` 的 `body` 单行时是 dict、多行时是 list of dict。
多位置断点编号是 `1.1`/`1.2`，要按 `.` 前缀归并。

**测试**：`test_break_insert_does_not_emit_a_notification`、
`test_console_break_does_emit_a_notification`、
`test_console_delete_notifies_with_id_not_bkpt`、
`test_breakpoints_single_row_is_a_dict`、`test_breakpoints_multi_location`。

### 7.7 gdb 会把断点滑到下一条可执行语句

在函数开括号行设断点，gdb 报回的 `line` 是第一条语句的行号。
**gutter 上的 `B` 显示的是 gdb 报告的行，不是光标的行** —— 这是正确行为，
C 版一致。E2E 测试因此断言 `marked >= cursor_line`，不是相等。
**不要"修"成显示光标行。**

### 7.8 Windows 路径的盘符与 `file:function` 语法冲突

`-break-insert -- "C:\dir\target.exe:main"` 会让 gdb 把 `C` 当文件名。
用函数名（`-break-insert main`）或 `mi.breakpoint_location()` 的
`"完整路径:行号"` 整体引号形式。

### 7.9 ptk API 的三个具体坑

| 坑 | 正确写法 |
|---|---|
| `KeyPressEvent` 没有 `.key_press` | `event.key_sequence[-1]` |
| `WindowRenderInfo` 没有 `.visible_line_count` | `len(ri.displayed_lines)` |
| `BufferControl` 没有 `prompt=` 参数 | `input_processors=[BeforeInput(lambda: [...])]` |
| `Application` 没有 `.run_test()`（3.0.52） | `create_pipe_input()` + `run_async()` |
| `FormattedTextControl` 默认不可聚焦 | `focusable=True`，否则 `Layout.focus()` 静默失败 |
| `App.refresh()` 不可 await（这是 Textual 的，不是 ptk） | — |

### 7.10 ptk 在 Windows 上少报一列，右边缘永远画不到

`Win32Output.get_size()` 把可见宽度算成 `srWindow.Right - srWindow.Left`
—— `srWindow` 是 **0 基闭区间**，这个差值比真实列数少 1，再被
`min(dwSize.X - 1, ...)` 封顶。布局于是按 N-1 列排，渲染器从不画最后一列，
**分隔栏/状态条、选中行都在右边缘缺一格**（C 版整行都画，见
`interface.cpp:446-451`）。实测 120 列控制台里状态栏背景只到第 118 列。

修法：`app.py:_console_output()` 在 `Windows10_Output` 上把
`win32_output.get_size` 换成 `Right - Left + 1`。全屏 `Application` 的渲染
全程关着 autowrap（首帧 `[?7l`，退出才恢复），写最后一列不会滚屏，安全。
**验证必须到控制台属性层**（`ReadConsoleOutputW` 逐列看属性），VT 抓包
会被 ConPTY 的再序列化骗过。回归测试：
`test_app.py::test_console_output_reports_the_true_width`。

### 7.11 PYTHONHOME 会毒害 gdb 内嵌的 Python

vendor gdb（mingw32 的 17.1）自带内嵌 CPython，启动时用 `PYTHONHOME`
定位它的 stdlib。cgdb.py 的用户恰好是配了 PYTHONHOME 的人群，指到无关
解释器时 gdb 启动即报 `Python initialization failed` 且后续命令全错。
修法：`gdbclient.py:_child_env()` 起子进程前剔除 `PYTHONHOME`（gdb 自己
的前缀决定它的 stdlib，这个变量对它永远不可能是对的）。
回归测试：`e2e/test_e2e.py::test_pythonhome_in_our_env_does_not_break_gdb`。

---

## 8. 未实现项与接入点

按「补上它的价值 / 工作量」排序。每项都给出确切的落点。

### 8.1 GDB 窗 Tab 补全 ⭐ 最值得做

**为什么值得**：这是 Python 版可以**超过** C 版 Windows 移植的地方。
C 版因为手写了行编辑器，补全要自己实现整套 UI（`win_specs.md` §11.2 列为已知限制）；
这里 ptk 的 `Completer` 接口天生适配。

**接入点**：
1. `app.py:_build_ui` 里给 `BufferControl(buffer=self.gdb_buffer, ...)`
   加 `completer=` 参数。
2. 实现一个 `Completer` 子类，`get_completions(buffer, document)` 里调
   `-interpreter-exec console "complete <line> <cursor>"`。
   ⚠️ **必须异步**：`get_completions` 可以是 async generator，
   用 `client.send()` + 回调，**绝不能**在里面 `call()`（那是主线程，见 §2.2）。
3. `complete` 命令的输出是 `~` 流记录里的候选列表，需要解析。
   先用 `CGDBPY_MILOG` 抓一次真实输出再写解析。
4. 补全期间要抑制 `complete_while_typing`（现在是 `False`，保持）。

**验证**：加 E2E 用例，在 GDB 窗敲 `bre` + Tab，断言屏幕出现 `break`。

### 8.2 `hlsearch` 渲染

配置项已存在并生效于 `Config`，但 `_source_text` 没用它。

**接入点**：`app.py:790 _source_text`。`self.view.search_pattern` 已经是编译好的
正则。对每行 body 做 `finditer`，把命中区间切成
`(style, pre)` / `(style + " class:search-match", match)` / `(style, post)` 三段。
`highlight.py` 的 `COLOR_STYLE` / `MONO_STYLE` 里 `search-match` 已经定义好了。

⚠️ 与高亮 token 叠加时要小心：token 边界和匹配边界不对齐，需要先把 token 序列
按匹配位置切开。C 版的做法见 `win_specs.md` §8.1（pre/match/post 三段渲染）。

### 8.3 `syntax` 强制语言

配置项已存储，`SourceFile.lexer()`（`source.py:88`）目前只按扩展名 + `guess_lexer`。

**接入点**：`SourceFile` 需要一个 `forced_language` 字段，`lexer()` 优先用它
（`pygments.lexers.get_lexer_by_name`）。`app._apply_config()` 里把
`config.get("syntax")` 推给当前 `self.src` 并触发重新 lex。

⚠️ 重新 lex 是 28 µs/行，大文件必须走 `highlight_async`。

### 8.4 文件选择对话框（`o` 键）—— ✅ 已实现

按 §8.4 原接入点实现：`cgdbpy/filedlg.py`（模型+搜索）+ `app.py` 的
`FOCUS_FILEDLG`。对话框激活时 `_build_root` 直接返回全屏的
`filedlg_window`（等价 if_draw 的 FILE_DLG 分支——不是覆盖层），
`set_focus` 检测跨越对话框边界时重建布局树。数据源
`-file-list-exec-source-files`，键位逐条照 `filedlg_recv_char`
（含 `gg`、数字+G；**裸 `G` 无动作**是照抄 C 的行为）。
一处偏差：C 在对话框内用 capture_regex 在状态栏逐字符捕获，
这里复用 `:` 命令行输入 `/` 模式（与 GDB 窗搜索同构）。

### 8.5 反汇编窗

配置项 `disasm` 已存在但无实现。C 版走 `DISASSEMBLE_PC` / `DISASSEMBLE_FUNC`
（`reference.md` §5.2），**输出不走 MI result 而走 console 流**，
需要在请求期间逐行累积并解析行首地址维护区间（`tgdb.cpp:374-413`）。

⚠️ 这个"输出改道"的语义很容易漏。要做的话先读 `reference.md` §5.2 那段。

### 8.6 多源码缓冲

C 版 `sources.cpp` 管理多个 buffer 并可切换。本版 `self.src` 是单个。

**接入点**：把 `self.src` 换成 `dict[path, SourceFile]` + `self.current`。
`goto_file_line`（`app.py:1174`）已经是"同文件则移动、否则打开"的结构，
改造成本主要在高亮缓存的生命周期管理。

### 8.7 鼠标

`Application(mouse_support=False)`（`app.py:_build_ui`）。
打开后需要给两个窗格加 `Window(..., ...)` 的鼠标处理，
并处理与 ptk 默认鼠标绑定（焦点切换、Buffer 光标定位）的冲突。
**优先级低**：C 版也没有。

### 8.8 横向滚动与控制台内搜索（2026-10 两轮审计，同日补齐并纠偏）

第一轮逐键对照后补齐、第二轮全量审计（§8.10）后**纠正语义**的清单：

- ✅ **源码窗 `h`/`l`**（`source_hscroll`）：`SourceView.left` +
  `_source_text` 的逐 token 列偏移（gutter 不动，只滚正文）；
  右向钳制到最长行（`sources.cpp:1091-1110` 的
  `max_width - width + lwidth + 6`），最短行文件滚不动。
- ✅ **GDB 窗滚动模式的 `h`/`l`/`0`/`$`**：**移动滚动光标列**
  （`scr_left/scr_right`，`scroller.cpp:254-284`），窗口不横向平移
  ——vterm 是折行不是横滚。第一轮实现的「整窗平移」是对
  `interface.cpp:955-962` 的误读，已纠正。
- ✅ **GDB 窗滚动模式的 `n`/`N`/`/`/`?`**：`ConsoleModel.search`
  从**滚动光标位置**起扫（同行第二个匹配可达）、按 `wrapscan`
  回绕、`?` 之后 n/N 记忆方向（`regex_direction_last`）；
  命中行放到光标行上。
- ✅ **`/` 输入时的递进跳转**：`cmd_buffer.on_text_changed` 钩子，
  每敲一个字符就搜（部分正则编译失败时原地不动）——源码窗、
  console、filedlg 三处都有（C 侧分别是 `status_bar_regex_input` /
  `gdb_input_regex_input` / `filedlg_capture_regex`）。
- ✅ **`:shell`/`:sh`**：`run_in_terminal(spawn)` —— ptk 的
  `in_terminal` 会 `input.detach()` + `cooked_mode()`，让子 shell
  独占控制台再整屏重绘；仅事件循环运行时可用（头部报错降级为
  状态栏消息）。⚠️ `run_in_terminal(func)` 返回 Future、**不是**
  上下文管理器，用错只得到一条 TypeError。子 shell 退出后按
  C 的 `run_shell_command` 等 "Hit ENTER to continue..."。
- ✅ **`:syntax` / `:set syntax=<lang>`**：`SourceFile.force_lexer`
  （Pygments 别名；`off`/`no`→TextLexer、空/`unknown`/`on`/`yes`
  →自动探测——后两者是「开高亮」不是语言名，`cgdbrc.cpp:669-672`），
  对当前文件重 lex、之后打开的文件也生效。
- ✅ **`o` 文件对话框**：见 §8.4。
- ❌ 仍缺：**`:logo`**（整屏 logo，纯装饰）、**`:highlight`**
  （约 50 个配色组的运行时覆盖体系）、**hlsearch 持久高亮与
  INCSEARCH 反色高亮**（递进跳转已有，见 §8.2）、**多源码缓冲**
  （§8.6）、反汇编（§8.5）、鼠标（§8.7）、**GDB 窗 Ctrl-R 增量
  历史搜索**（ptk 需要 SearchBufferControl 布局接入）、
  **gdb `set prompt` 不跟随**（`(gdb) ` 硬编码）。

### 8.8b inferior 全屏程序

需要终端模拟器（`pyte`）+ ConPTY，等于把 C 版故意绕开的 `libvterm` 请回来。
**建议不做**，与 C 版 Windows 移植保持同样的限制（`win_specs.md` §11.7）。
inferior 的输出按哑终端模型摄入（CSI/OSC 转义序列会被消费掉，颜色不渲染
但不会显示 `[31m` 残渣；宽字符按 2 列折行）。

### 8.9 `:map` 在 GDB 窗生效（imap）

`KeyManager` 已经有 `insert_set` / `set_insert_mode()`，但 `app.py` 只用了
`map_set`。C 版用 `imap` 覆盖 GDB 窗场景。

⚠️ 要做的话得先解决一个设计冲突：GDB 编辑态下普通字符归 ptk Buffer
（`_buffer_owns_keys()`），kui 根本看不到它们。要么放弃 Buffer 自己实现行编辑
（那就丢掉了 §3.9 说的"免费行编辑器"），要么只在 Buffer 的
`accept_handler` 之前对整行做映射替换。**建议后者**，但语义与 C 版的逐键 imap
不完全等价，要在文档里写清。

已对齐的部分：映射集随焦点切换（source→`map_set`、GDB→`imap_set`、
filedlg/命令行→清空，`cgdb.cpp:427-432`），`normal :map` 不再泄漏进文件
对话框。**映射 rhs 穿越焦点切换**（2026-10-19 修复）：C 的
`kui_ctx::set_map_set` 只换指针不清缓冲，移植版曾把 pending 键清掉，
`:map <f4> iinfo locals<cr><esc>` 这类"切窗口+打命令+回来"的映射在首个
`i` 切焦点后整条 rhs 被吞；现对齐 C 不清缓冲，且 `gdb_key` 的 default
分支把映射注入的普通键/回车/退格写进 gdb 编辑缓冲（终端直打的键仍归
ptk Buffer，到达该分支的只可能是映射展开）——`<cr>`=`validate_and_handle`、
`<esc>` 走 mode-key 回源码窗。映射 rhs 里的其他控制键（Ctrl-A 等）暂丢弃。

### 8.10 第二轮全量审计的对齐（2026-10-19，本次提交）

对 C 原版逐行审计（8 个模块、~60 条差异）后落地的对齐，按域分组；
完整审计报告存 `.tmp/audit/REPORT.md`（临时件，结论已并入本节）：

**源码窗显示层**（`sources.cpp:760-1058` 重刻）
- 视口每帧由光标推导：`sel_line - height/2` 居中，短文件整块居中；
  顶部派生行存回 `view.top` 供测试。
- gutter = `log10+1` 位行号 + 竖线列 + 标记列；竖线列有焦点时加粗
  （焦点指示之一）。
- 断点没有专用字符——**行号染色**：红=enabled、黄=disabled，
  优先于执行行（绿）/选中行（白）行号色；同表末条胜出（C 的覆盖序）。
- 执行行默认 `longarrow`：竖线列换 `├`，`─` 伸到首个非空白列再 `>`；
  选中行**本移植默认 `highlight`（整行上色）——C 默认是 `block`**
  （缩进处反色块），但 block 标记与行尾的硬件光标并排会被读成
  "两个光标"，且 block 不随 h/l 滚动而硬件光标会（用户实测反馈）；
  C 的形态保留为 `:set sld=block`。
  `executinglinedisplay`/`selectedlinedisplay`/`arrowstyle`（弃用别名，
  不接受 `block`）全部接线。
- 文件尾 `~` 行；mark 画在竖线列（HLG_MARK，白粗）。
- 源码窗**有焦点时隐藏系统光标**（`always_hide_cursor` 为
  `Condition(focus==source)`，每帧求值）：光标条就是位置指示器，系统光标
  与光标条并排会被读成"两个光标"（用户偏好；C 在源码窗保留
  `curs_set(1)`）。GDB 编辑行/命令行/滚筒光标照常显示。
  ⚠️ 实测陷阱两条：**ConPTY 不把隐藏光标的定位镜像进 VT 流**（渲染器内部
  `renderer._cursor_pos` 正确，流里光标停在最后绘制的单元）——e2e 因此
  改用两个不依赖光标的信号：**竖线列加粗**（`class:vline-focus`，焦点
  判定，重绘/resize 都携带）与**光标条背景色**（选中行定位，
  `48;2;46;74;98`）；ConPTY 对 `?25l` 的转播也不稳定（真实控制台没有
  这层中继，不受影响）。另：ptk 的 vt100 层认为已隐藏时会跳过
  `?25l` 重写，resize 后 ConPTY 恢复光标就永远不再隐藏——已用
  强制每次都写的 `hide_cursor` 包装处理（`_console_output`）。

**配置系统**
- `set no<opt>` 剥前缀清布尔（仅布尔，`set notabstop` 报错）；
  `set opt` 无值：布尔置 1，int/string 汇报当前值（C 报错；
  `:set ts` 决不能把 tabstop 设成 1）。
- 选项名大小写敏感（C 是 strcmp/bsearch），值大小写不敏感（strcasecmp）。
- **rc 文件是完整命令语言**（`command_parse_file`）：注释可出现在行中、
  `\` 续行、行首 `:` 可选；`set` 行在首帧前生效，其余命令在 gdb
  启动后经 `_do_colon` 重放（`run_rc_commands`），错误逐行报告进
  console（`Error parsing line N: ...`）且不中断。

**GDB 窗与按键**
- ESC 非急切（eager）：等 `app.timeoutlen`（=ttimeoutlen，ttimeout/
  timeout 开关接线，双双关闭则 None=永不 flush），使
  `ESC+b/f/d/Backspace` = Alt 词操作（ptk 的 `escape,b` 序列绑定，
  app 级优先）成立；孤立 ESC 才是 cgdbmodekey。
- F1/F5/F6/F7/F8/F10/Ctrl-L 只在源码窗焦点生效（C 在 `cgdb_input`）；
  GDB 焦点下吞掉（C 发给 gdb，MI 通道不能塞原始转义序列）。
- F5 恒 `-exec-run`（inferior 运行中先 interrupt 再 run，C 语义=重启）。
- 冒号行退格删掉 `:` 前缀即取消（C 空行退格=退出状态栏）；删前缀后
  输入的内容绝不会当 gdb 命令发出。
- `/`+回车空模式重放上次搜索；ESC 取消恢复光标/滚动位置
  （`orig_line_regex` / `scr_disable_search(false)`）；`?` 后 n/N 方向记忆。
- gdb 空闲时 ^C 清空本地编辑行（readline 的 ^C 语义）；
  空行 Ctrl-D 发 `-gdb-exit`；冒号命令名大小写敏感、
  `:q!`/`:h` 不再是命令（C 词法如此），`Unknown command:` 报整行。
- cgdbmodekey 接受单字符与 `<X>` 记法；Tab/Ctrl-G 设为模式键时
  条件绑定接管。
- inferior 运行期间按键逐字节直通 tty（`tgdb_send_char` 语义），
  回显不带 `(gdb) ` 前缀。

**协议联动**
- `=thread-selected` 与 `*stopped` 同路：解析 frame → 跳源码位置。
- `u` 恒发 `until "file":line`（tgdb 传显示文件路径）；
  `gdb_up/down` 改走 console（MI 没有 `-exec-up`）。
- 断点行匹配先比 gdb 拼写再比 abspath（fullname 缺失的相对 `file`
  也能对上）；重开当前文件保视口（`:e` 只在 mtime 变了才重读，
  filedlg 回车不重置）；`autosourcereload` 接线（每次停止时重读）。
- inferior tty resize 跟随 GDB 窗（POSIX pty 路径）。

**console**
- 滚动模式 j/k：光标先走、窗口后滚（`scr_up/down`），滚动光标可见
  （console 窗口成为 GDB 焦点下的焦点控件）；`[d/max]` 叠加在
  第 0 行右缘。
- 内容不足一屏顶对齐（vterm 从 0 行写起）；ESC 离开 GDB 窗保留
  滚动状态（C 保留，之前是有意偏离——滚动光标可见后按 C 对齐）。
- 非滚动模式 PageDown 不再进滚动模式（C 发给 gdb）。
- 转义序列消费、CJK 按显示列折行；`:set scrollbackbuffersize`
  运行期只存不裁（C 只在创建时读）。

**filedlg**
- 过滤磁盘上不存在的文件、C 式三桶排序（`.`相对、普通、绝对，
  组内 strcmp）、去重；显示 gdb 原始拼写。
- 搜索：硬编码忽略大小写、回绕、方向记忆、逐字符递进；
- 布局：label + `H-2` 列表行 + 底部状态行（对话框消息住那里）；
  选中行=行号加粗 + `->` 箭头。

**状态栏**（`update_status_win` 重刻）
- 内容=当前文件完整路径；消息**替换**整行到下一键为止；
  超宽 `>`+尾截断。C 源码里 GDB 焦点的 `*` 是死代码
  （被 if_display_message 重画抹掉；官方 cgdb 实测无 `*`），
  Python 版同样没有。焦点可观察信号=终端光标位置
  （源码窗选中行 / GDB 编辑行 / 命令行）。

**CLI**
- `-v`/`-h` 短选项；无控制台（mintty/管道）启动给出一条干净错误
  而非 ptk 裸 traceback。

---

## 9. 测试规格

### 9.1 分层原则 ⚠️

**本项目四个最严重的 bug（§7.1 / §7.3 / §7.5 / 7.9 第一条）单元测试全部抓不到。**
原因是一致的：单元测试直接调 `CgdbApp.on_key()` / `_source_text()` 传假对象，
绕过了 prompt_toolkit 的真实事件循环、真实渲染和真实终端。

**推论**：
- 任何涉及**按键、渲染、焦点、时序**的改动，**必须**加或改 E2E 用例。
- 任何涉及**真实 gdb 语义**的改动（新 MI 命令、断点、停止事件），
  **必须**加 `test_gdb_integration.py` 用例，不能只靠 `FakeGdb`。
- `FakeGdb`（`test_app.py`）只适合验证"UI 发出了正确的命令"和
  "回调正确更新了状态"，**不能**验证 gdb 真的会那样回复。

### 9.2 kui 的黄金参考

`tests/data/kui_oracle.json` 是**用真实的 C `kui_driver` 跑出来的**，
不是手写的期望值。生成方式：pty 里启动
`lib/kui/kui_driver --file <map>`，按 `basic.exp` 的顺序喂同样的 198 组输入，
逐用例抓 stderr。

两种比对都在跑：
- **全序列**：198 个用例的输出连起来（去掉 `\r\n(kui) ` 提示符）逐字符比对。
  这与抓取时序无关，是权威校验。
- **逐用例**：189 个"干净"用例单独断言。剩下 9 个因为 kui 的 1000 ms 映射超时
  会把输出串到下一个用例的抓取窗口，只参与全序列比对。
  `_clean_indices()` 的判据是「本用例以提示符结尾且只含一个提示符，
  **且前一个用例也满足**」。

**重新生成 oracle**（改了 `keycodes.py` 的表之后必须做）：
```bash
# WSL2 Ubuntu-24.04，需要已构建的 kui_driver
wsl.exe -d Ubuntu-24.04 -- bash -c 'cd /home/skywind/tmp && \
  python3 /mnt/e/Github/Tools/cgdb/.tmp/pyver/oracle_kui.py > kui_oracle.json'
# 然后拷到 python/tests/data/kui_oracle.json
```
生成脚本当前在 `.tmp/pyver/oracle_kui.py`（**未入库**，`.tmp/` 被 gitignore）。
⚠️ 如果你要长期维护 kui，**把这个脚本挪进 `python/tests/` 并提交** ——
它是黄金参考的唯一再生手段。抓取时 `settle` 必须 > 1000 ms（kui 的映射超时），
否则输出会串用例。

⚠️ 生成脚本里**必须跳过 `<C-z>` 用例**：`kui_driver` 把 Ctrl-Z 当作
"进入映射编辑模式"，会吞掉之后所有输入。`basic.exp:417` 也明确跳过了它。

### 9.3 交叉验证型测试（不要削弱它们）

这几个测试的价值在于**比对独立来源**，而不是自证：

| 测试 | 独立来源 |
|---|---|
| `test_kui.py` 的 oracle 比对 | 真实 C `kui_driver` 的输出 |
| `test_cgdbrc.py::test_every_c_option_name_is_present` | 正则抓 `cgdb/cgdbrc.cpp` 的选项表 |
| `test_source.py::test_cached_highlighting_matches_a_full_file_lex` | 直接用 Pygments 全文 lex 作参考 |
| `test_source.py::test_windowed_lexing_is_demonstrably_wrong` | 反证：证明朴素方案错 |
| `test_gdb_integration.py` 全部 | 真实 gdb 17.1 的实际回复 |
| `test_e2e.py` 全部 | 真实 ConPTY 屏幕的 VT 解析结果 |

**改这些测试时，问自己"它还在比对独立来源吗"**。
把断言放松成"不崩就行"等于删掉测试。

### 9.4 E2E 基础设施

`tests/e2e/conpty.py`：
- `ConPtySession` —— ConPTY 接线（配方来自 `porting-feasibility.md` §3.1：
  属性表传句柄值、`STARTF_USESTDHANDLES` + NULL std handles、
  `ClosePseudoConsole` 会阻塞所以要放到独立线程）
- `VTScreen` —— VT100 屏幕模型，`feed()` 解析、`text()` / `line(i)` / `find()` 断言
- `KEYS` —— 键名到转义序列的映射

⚠️ **`VTScreen.csi()` 必须跳过私有模式序列**（`?` / `>` 前缀，如 `\x1b[?25l`）。
第一版没跳，`int("?25")` 抛异常把读线程打死，屏幕全空。
现在解析异常被捕获并记进 `session.parse_errors`，
`test_no_vt_parse_errors_during_a_session` 断言它为空 ——
**否则其他所有 E2E 断言都是在读一块损坏的屏幕**。

⚠️ **断言焦点不要用子串匹配**：`"CGDB  target.c"` 包含 `"GDB  target.c"`，
`wait_for("GDB  target.c")` 会在焦点还没切的时候就通过。
用 `Cgdb.focus()` / `wait_focus()`（解析状态行首 token）。

⚠️ **E2E 里等 ESC 要留够时间**：ESC 有 ~107 ms 的固有延迟（§7.3），
`settle(0.3)` 会 flaky。用 `wait_focus(..., timeout=...)` 轮询而不是固定 sleep。

### 9.5 加新测试的落点

| 改了什么 | 加在哪 |
|---|---|
| MI 解析 / 新记录类型 | `test_mi.py`（纯解析，无需 gdb） |
| gdb 实际回复形状 | `test_gdb_integration.py`（**必须**真 gdb） |
| kui 匹配 / 记法 | `test_kui.py`，并考虑是否要重生成 oracle |
| 行模型字节语义 | `test_console.py`，对照 `win_specs.md` §8.1 的表 |
| 高亮 / 编码 / 断点表 | `test_source.py` |
| `:set` 选项 | `test_cgdbrc.py`（记得同步 `_SPEC` 和 C 版） |
| UI 状态机（焦点/导航/命令） | `test_app.py`（`FakeGdb` 够用） |
| 按键、渲染、时序、真实交互 | `tests/e2e/test_e2e.py`（**只有这里能测**） |

---

## 10. 调试钩子

两个环境变量，都是 append 模式、带时间戳：

```bash
CGDBPY_KEYLOG=E:\path\keys.txt   # 每个到达 on_key 的按键：时间戳 + ptk key + data + cgdb_key
CGDBPY_MILOG=E:\path\mi.txt      # 每行 MI 收发：时间戳 + "->"/"<-" + 内容（截断 400 字符）
```

实现：`app.py:124 _debug_key` / `gdbclient.py:32 _milog`。
**开销为零**（未设置时直接 return），可以留在代码里。

§7.1 的死锁靠 MILOG 定位（流量停在 `6-break-info`），
§7.3 的 ESC 延迟靠 KEYLOG 的发送/到达时间差定位，
§7.9 第一条靠 KEYLOG 完全没有输出定位。

⚠️ **路径要用 Windows 形式**（`E:\...`）。从 Git Bash 传 `/tmp/x.txt` 的话，
Windows 侧的 Python 会把它解释成当前盘符下的 `\tmp\x.txt`，
而你在 Git Bash 里 `cat /tmp/x.txt` 看的是另一个地方。

**其他定位手段**：
- ptk 会吞掉 handler 里的异常 → 界面"毫无反应"。
  `GdbClient._fire()` 会把回调异常打到 stderr；
  但**键绑定 handler 的异常不会**，要靠 E2E 或手动在 ConPTY 里跑。
- 想看真实屏幕：`ConPtySession.raw` 是完整 VT 字节流，dump 出来 `repr()` 看。

---

## 11. 禁忌清单（不要"修"这些）

每一条都是有意的设计，看起来像 bug 但不是。

1. **不要把 `terminal_map_set()` / `KeyManager` 的终端层删掉**，
   即使 `app.py` 没用它。它是 359 项 kui 测试的对象，也是 `basic.exp`
   198 项规格的一部分（见 §3.9）。
2. **不要把高亮改成窗口化 lex**（§3.6，有反证测试）。
3. **不要把 `-gdb-set inferior-tty` 的参数加上引号**（§7.2，会静默失效）。
4. **不要把 `-break-insert` 的位置改成只引路径**（§7.2，会报 parsing 错误）。
5. **不要把 `Keys.Escape` 的 `eager=True` 去掉**（§3.9，会完全不触发）。
6. **不要把 `ttimeoutlen` 调回 ptk 默认的 0.5**（§7.3，ESC 会变成 500ms）。
7. **不要在回调里避免调用 `call()`** —— 那是合法的（§2.2）。
   但**不要**在主线程（ptk 事件循环）里调 `call()`。
8. **不要把 gutter 的 `B` 改成显示光标行**（§7.7，gdb 会滑动断点位置）。
9. **不要为了"和 dejagnu 一致"复刻 `basic.exp` 的那个空过用例**（§5）。
10. **不要给 `parse_line` 加裸数字容错**（§3.1 不变量 2，会掩盖协议错误）。
11. **不要用 `git add -A`**：这个仓库常有并行开发的未提交改动
    （`testsuite/win32/`、`lex.yy.c` 等）。只 stage 自己的文件。
12. **不要把窗格内容按固定 budget 补齐**（§7.5）。

---

## 12. 提交前检查清单

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"
cd python

# 1. 快测试（2 秒）
python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/test_gdb_integration.py

# 2. 全测试（2.5 分钟）—— 提交前必跑
python -m pytest tests/ -q

# 3. 手动冒烟：真的能起来、能设断点、能单步、能退出
python cgdb.py -d D:\Dev\mingw32\bin\gdb.exe --source cgdbpy/mi.py -- -q <某个32位exe>
#    i → break main → run → F8 几次 → ESC → :q

# 4. 改了 keycodes.py 的表？重新生成 kui oracle（§9.2）
# 5. 改了 cgdbrc.py 的选项？确认 test_every_c_option_name_is_present 还过
# 6. 改了 MI 命令拼写？跑 test_gdb_integration.py 全部
# 7. 改了按键/渲染/焦点？必须有 E2E 覆盖

cd ..
git status --short          # 确认只有自己的文件
git add <具体文件>           # 不要 -A
git commit -s               # 仓库约定要 signed-off-by
```

**不要提交**：`.tmp/`、`lex.yy.c`、`__pycache__/`、`.pytest_cache/`
（后两者已被 `python/.gitignore` 覆盖）。

---

## 13. 性能基线与实测数据（改动后对照自检）

如果某项显著变差，先找根因再提交。测量脚本见附录 A。

### 13.1 端到端指标

| 指标 | 基线 | 测量方式 |
|---|---|---|
| 启动到首帧（用户感知） | **~260 ms** | 解释器 31 + import ptk 131 + 首帧 98 |
| ESC 键端到端 | **~107 ms** | `CGDBPY_KEYLOG` 时间差（受 `ttimeoutlen=100` 支配，§7.3） |
| 普通键端到端 | 0–4 ms | 同上 |
| ptk 全屏渲染一帧 | 1.98 ms（p95 2.71） | `bench_app_ptk.py` |
| ptk 持续帧率 | 73.5 fps，合并比 456:1 | 同上 |
| `python -c pass` | 31 ms | — |
| `import prompt_toolkit.application` | 162 ms | — |
| `import textual.app`（对照） | 190.6 ms | — |
| `import pygments`（对照） | 0.6 ms（惰性） | — |

对照参考：**C 版 `cgdb.exe --version` 是 8.5 ms**（6.6 MB 全静态）。
Python 版在每一个性能指标上都更差，这是接受的代价（理由见 §19）。

### 13.2 GDB/MI 通道（`gdb.exe` 17.1 mingw32，匿名管道）

| 项 | 数值 |
|---|---:|
| 启动到首个 prompt | 81.8 ms |
| `-break-info` 往返（token 同步，200 次） | 中位 **117 µs**，p95 12.1 ms，max 24.5 ms |
| 其中 Python 侧唤醒开销 | 中位 **30 µs**，p95 50 µs |
| `-file-list-exec-source-files` | 89.2 ms |
| 200× `-break-insert` | 124.7 ms（623 µs/条） |
| `run` → `*stopped`（断点命中） | 85.5 ms |
| `continue` → `*stopped`（退出） | 30.2 ms |
| `parse_line` 成本 | 1.08 µs/行 → **925,412 行/s** |

p95 12 ms / max 24 ms 的长尾在 **gdb 侧**，C 版一样有，不要在 Python 里"修"它。
Windows 的 15.6 ms 定时器粒度与此无关（实测 `Event.wait` 请求 1 ms → 1.43 ms 返回）。

### 13.3 大负载摄入

| 命令 | 负载 | 耗时 | 吞吐 |
|---|---:|---:|---:|
| `info functions .` | 593.5 KiB / 15,094 行 | 296.9 ms | **2.0 MB/s，50,837 行/s** |
| `disassemble main` | 1.5 KiB / 34 行 | 1.1 ms | 1.4 MB/s |

593 KB 的极端输出 300 ms 内吃完，其中大部分是 gdb 自己在生成。

### 13.4 Pygments 语法高亮（对本仓库真实源文件，Pygments 2.20.0）

| 文件 | 行数 | 全文 lex | µs/行 | 行/秒 |
|---|---:|---:|---:|---:|
| `lib/tgdb/gdbwire.c` | 8,194 | 177.8 ms | 21.7 | 46,096 |
| `lib/vterm/state.c` | 2,266 | 93.0 ms | 41.0 | 24,376 |
| `cgdb/interface.cpp` | 1,866 | 50.6 ms | 27.1 | 36,869 |
| **6 个文件合计** | **17,266** | **484.5 ms** | **28.1** | **35,635** |

- 只 lex 可见窗口：40 行 **0.216 ms**（4,624 次全重绘/秒），100 行 1.168 ms
  —— 但**这个方案是错的**，见 §3.6
- `style_for_token()`：1.64 M tokens/s，可忽略

⚠️ 别用 `D:\Dev\mingw32\bin\python.exe`（那是 mingw32 自带的 3.14.3）跑基准，
上面所有数字都是 `C:\Share\Python311\python.exe`（3.11.9）。

---

# 第二部分：设计依据与决策记录

> 这一部分回答「为什么是这个样子」。日常改代码不需要读；
> 但**要推翻某个既有决策、或做架构级改动之前必须读** ——
> 第一部分里很多看起来像 bug 的东西（§11 禁忌清单）理由都在这里。
>
> 结论基于 2026-10-19 在本机（Windows 10 / Python 3.11.9 / GDB 17.1 mingw32）的实测。

## 14. 结论与代价

**做 Python 版是可行的，而且比 C 版的 Windows 移植更容易。** 三个理由：

1. **架构难题已经被 Windows 移植解掉了。** 原版 cgdb 依赖
   `gdb --nw -ex "new-ui mi /dev/pts/N"` 开出**第二条 MI 通道**（`fork_util.cpp:226`），
   主控制台仍走 pty + gdb 自己的 readline，其输出字节流（含 readline 的光标转义序列）
   由 libvterm 终端模拟器还原成屏幕。`windows` 分支已经证明：
   `new-ui` 在原生 Windows GDB 上根本不可用（`doc/porting-feasibility.md` §3.2），
   改用 **`gdb --interpreter=mi` 单通道走管道 + 合成 GDB 窗口** 后全功能可用
   （`doc/win_specs.md` §7、§8，E2E 23/23）。Python 版直接继承这个架构，
   **一步跳过 pty、双通道和终端模拟器三座大山。**

   > 注意：`roadmap.txt` 里说「tgdb is still only capable of speaking the
   > annotation protocol」是**历史文档，已不成立**。实测全仓库
   > （排除 `win32/generated/`）只有 `gdbwire.c:7952` 和 `gdbwire.h:3181`
   > 两处注释提到 annotate，且都是在描述*别的*前端的用法。
   > **当前 cgdb 走的是 MI，不是 annotate。**

2. **Python 生态正好覆盖了 cgdb 最重的几块。** 全仓库手写 C/C++ 共 **41,255 行**
   （另有 26,209 行 flex 生成物在 `win32/generated/`，1,277 行 `.lpp` 词法源），
   其中约 **24,700 行是在重新发明 Python 已有的轮子**。详见 §17。

3. **性能不是障碍 [实测]。** MI 往返中位数 117 µs、Python 侧同步开销仅 30 µs；
   MI 大负载摄入 ~2 MB/s；ptk 全屏 diff 渲染 1.98 ms/帧、73.5 fps。
   唯一真实的性能坑是 Pygments 全文 lex（28 µs/行），已有明确解法（§3.6）。
   完整数据见 §13。

**代价**：启动从 8.5 ms 变成 ~260 ms；分发从 6.6 MB 单文件变成需要解释器或
20 MB+ 的打包产物（§18.1）。反过来，32 位 mingw 那套约束不再适用
（Python 版天然跨位数，是实质优势，§18.2）。

## 15. 架构路线的取舍

有三条本质不同的路线，选错了一条后面全白做。

### 15.1 路线 A：独立 TUI 进程 + GDB/MI 管道 ✅ **已采用**

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

### 15.2 路线 B：gdb 内嵌 Python 脚本（gdb-dashboard 模式）❌ 否决

[gdb-dashboard](https://github.com/cyrus-and/gdb-dashboard) 是一个 `.gdbinit` 文件，
用 **gdb 自带的 Python API** 在每次停住时打印一个仪表盘。

- **优点**：工作量极小（一个文件），不需要任何协议解析 —— `gdb.selected_frame()`、
  `gdb.breakpoints`、`gdb.parse_and_eval()` 都是活的 Python 对象。
- **致命缺点**：**gdb 自己的 readline 拥有终端**。你只能在 stop 事件里打印，
  无法实现 cgdb 的核心体验 —— 下半屏是一个可以独立滚动、可以 vim 式操作的
  GDB 会话窗，上半屏源码窗有自己的焦点和断点操作。
  路线 B 做出来的是「更好看的 gdb-dashboard」，不是 cgdb。

**如果哪天只想要「源码+局部变量+栈的自动展示」，路线 B 一天就能做完**，
值得作为低成本副产品记住。

### 15.3 路线 C：直接用 gdb 自带的 TUI ❌ 否决

`gdb -tui`（Ctrl-X A）已经有分屏。但它的源码窗不能 vim 式操作、没有断点可视化、
没有文件对话框、不能配置 —— 这正是 cgdb 存在的理由。

### 15.4 MI 解析：自写还是用 pygdbmi ✅ 自写

`gdbwire.c` 8,193 行是**外部项目 GDBWIRE 1.0 的 amalgamation**
（`gdbwire.h` 头部注明：Robert Rossi，git rev `6faba36`），不是 cgdb 自己写的。
它是一个**纯 MI 解析器**（`gdbwire_mi_*` 系列类型）。Python 版需要的正是同一件事，
而这件事在 Python 里只要几百行 —— 实测 `parse_line` 1.08 µs/行（§13.2）。

选**自写**（`cgdbpy/mi.py`，412 行）而不是 `pygdbmi`，理由：

1. **pygdbmi 半停滞 [实测]**：PyPI 最新 `0.11.0.0`，上传时间 **2023-01-29**，
   至今近三年没有新版本。（`requires_dist` 为空，零依赖这点是好的。）
   MI 协议稳定，400 行自己维护比依赖一个半停滞的库更可控。
2. **pygdbmi 是同步阻塞式 API**（`GdbController.get_gdb_response()`），
   接进本实现的三线程模型（§2.2）还得自己包一层。
3. 自写可以做到完全 token 化 + 与 `tgdb.cpp` 的命令队列语义一一对应。

### 15.5 为什么不需要终端模拟器

C 版带 `lib/vterm`（6,504 行）是因为 gdb 的 readline 会往 console pty 里写
光标移动/清行转义序列。**MI 模式下 gdb 根本没有 readline 在跑**，
输出是纯文本 —— 这正是 C 版 Windows 移植能用「哑终端行模型」替代 vterm 的原因
（`win_specs.md` §8.1）。Python 版继承同一结论，`libvterm` 整块消失。

例外：inferior 自己的全屏输出。要支持就得请回终端模拟器（`pyte`）+ ConPTY，
见 §8.8，**建议不做**。

## 16. TUI 选型：prompt_toolkit vs Textual

同一负载、同一 ConPTY（40 行 × 110 列）、同一台机器、同一 Python 3.11.9。
布局仿 cgdb：上半屏源码窗、下半屏 GDB 控制台窗。
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

**关于「灌入 5000 行」那一行的诚实说明**：ptk 侧是一次性
`buffer.text = "\n".join(lines)`（0.2 ms），Textual 侧是 5000 次
`RichLog.write()`（每次投递一条消息，636 ms）。**这是 API 形状差异，不是框架性能差异** ——
Textual 有批量的 `write_lines()`，用它数字会好看得多。
但值得注意的是 Textual 在 5000 次写入期间**只重绘了 2 帧**，
说明它的合并机制在突发输出下表现很好，这一点对 debugger 恰恰重要。

### 16.1 选 prompt_toolkit 的理由（按权重）

1. **架构契合**。cgdb 的灵魂是 kui 那套「可配置 vim 映射 + 原始字节透传给 gdb」。
   ptk 的 `KeyProcessor` 就是前缀树 + 超时，和 kui 同构；
   Textual 的按键是 action/command 模型，要在上面实现 `:map abc def` 得逆着框架走。
2. **渲染更快、输出更省 [实测]**。2 倍帧率，一半的 VT 字节。
   对 GDB 突发输出（`info functions .` 一次 593 KB）是实打实的余量。
3. **同域先例**。IPython / ptpython / pgcli / mycli 都是
   「全屏 + vim 键位 + 补全 + 流式输出」的 ptk 应用，这条路被走通过很多次。
4. **同步/异步都行**。GDB 输出突发时 ptk 可以同步处理完再渲染；
   Textual 强制 asyncio，且**消息泵是个陷阱**（§16.2）。
5. **依赖更轻 [实测]**。`prompt_toolkit 3.0.52` 的依赖列表就一项：`wcwidth`。
   `textual 8.2.3` 则要拖进 `markdown-it-py[linkify]`、`mdit-py-plugins`、
   `platformdirs`、`pygments`、`rich`、`typing-extensions`。
6. **Textual 的语法高亮不覆盖 C/C++ [实测]**。`textual[syntax]` extra 声明的
   tree-sitter grammar 是：bash、css、go、html、java、javascript、json、markdown、
   python、regex、rust、sql、toml、xml、yaml —— **没有 c，也没有 cpp**。
   对一个主要用来调 C/C++ 的调试器前端，这意味着要么自己额外装
   `tree-sitter-c`/`tree-sitter-cpp` 并手动注册 grammar，
   要么放弃 `TextArea` 的增量高亮改走 Pygments（那就等于自己实现 §3.6 的缓存方案，
   Textual 在这块的优势就没了）。

**代价**：没有 widget 工具箱，文件对话框、状态条、滚动条都要自己写。
但如 §16.3 所说，cgdb 的 UI 本来就极简，这部分工作量比听起来小。

### 16.2 [实测] Textual 的消息泵陷阱

第一次跑 Textual 基准，3.7 秒里只输出了 **410 字节** VT —— 一帧内容都没画。
dump 出来看，全是进/出备用屏的序列：

```
b'\x1b[?1015h\x1b[?1004h\x1b[>1u\x1b[?2004h\x1b[2J\x1b[m\x1b[H' + 40×'\r\n'
... 3.4 秒空白 ...
b'\x1b[?2004l...\x1b[?25h'      ← 直接 teardown
```

根因：基准写成了 `async def on_mount`。**Textual 在消息泵内部 await 消息处理器**，
所以一个长时间运行的 `on_mount` 会把整个泵堵死，compositor 永远拿不到渲染消息 ——
即使中间 `await asyncio.sleep(0)` 也没用，因为被堵住的是泵而不是事件循环。
改成 `def on_mount` + `create_task()` 后立刻正常（96 KB VT，36 fps）。

这对 debugger 是**结构性风险**，不是基准脚本的小毛病：cgdb 必须在
GDB 吐 593 KB 输出、inferior 疯狂打印、用户连续按键时保持响应。
Textual 模型下所有这类工作都必须正确切分到 worker 里，一处写错就是整个 UI 冻住
—— 而且**冻住时不报错、不崩溃，只是不重绘**，极难调试。
ptk 的同步渲染模型没有这个失效模式。

（对照 §7.1：本实现自己在 gdb 回调上踩过**同构**的坑 —— 阻塞的回调冻死会话。
这类"静默冻结"是异步 TUI + 子进程架构的共性风险，不是某个框架的缺点。）

### 16.3 cgdb 本来就没有窗口管理器

`roadmap.txt` 里上游作者自己写着：

> CGDB essentially has no window manager. A window manager is the main feature
> that CGDB requires in order to better be able to easily display more
> information to the user.

cgdb 的 UI 就是：**两个可滚动文本窗格 + 一条状态行 + 一个 `:` 命令提示 +
一个文件对话框**。没有布局引擎、没有嵌套分割、没有鼠标。

这正好是 prompt_toolkit 的甜区，也正好是 Textual 的 widget/CSS 机制**用不上**的地方。
选 Textual 等于为了一个不需要的布局引擎付出 2 倍的渲染成本和一套异步消息泵的复杂度。

### 16.4 其他候选（均已否决）

- **urwid**：成熟、list-based widget、事件循环无关。但 API 老化、社区活跃度低、
  没有 vim 键位机制，相对 ptk 没有优势。本机未安装，**未实测**。
- **py_cui / asciimatics / blessed**：更底层或更小众，都要自己写更多。
- **curses（windows-curses）**：能最直译 C 版（`sys_win.cpp` 的 `swin_*` 抽象层
  几乎可以一对一映射），但 Windows 上的 curses 是二等公民
  （C 版正是因为这个才选 PDCurses-wincon），且拿不到 ptk 的输入处理。
- **Rich**：不是 TUI 框架，是渲染库。可以作为 ptk 的补充做富文本，
  但单独用它做不了全屏交互应用。

## 17. 模块映射与工作量

LOC 统计口径：`git ls-files` 里的 `.c/.cpp/.h`，**不含** `win32/generated/`
的 26,209 行 flex 生成物。「代码行」= 去掉空行和纯注释行。

| C 模块 | 原始行 | 代码行 | Python 替代 | 实际代码行 | 说明 |
|---|---:|---:|---|---:|---|
| `lib/tgdb/gdbwire.c+h` | 11,409 | 5,486 | 自写 MI 解析 | **412** | §15.4，最大的一笔节省 |
| `lib/tgdb/tgdb.cpp+h` | 2,852 | 1,665 | MI 客户端 + 命令队列 + 回调 | **387** | 命令映射表见 `reference.md` §5.2 |
| `lib/vterm/` | 6,504 | 4,910 | **不需要** | **0** | §15.5 |
| `lib/tokenizer/` + `.lpp` | 459+1,277 | 294 | Pygments | **128** | 只写 token→样式映射 |
| `lib/kui/` | 2,627 | 1,364 | 前缀树 + 记法表 | **733** | kui.py 407 + keycodes.py 326 |
| `lib/rline/` | 171 | 85 | prompt_toolkit 原生 | **0** | rline 本来就是导出 readline 键表给 kui |
| `lib/util/` | 4,845 | 2,921 | 标准库 + 命名管道 | **276** | 只剩 inferior tty 需要平台代码 |
| `cgdb/` UI 层 | 11,985 | 7,286 | ptk 界面 + 行模型 + 配置 | **1,849** | app 1215 + console 251 + source 361 + cgdbrc 222（部分） |
| **合计** | **40,852** | **24,011** | | **3,576** | **约 6.7 倍缩减** |

（仓库手写 C/C++ 总计 41,255 原始行 / 24,291 代码行；上表少的 403 / 280 行是
`lib/tgdb/driver.cpp` + `test.cpp` 两个测试 driver，Python 版换成 pytest，不计入。
「实际代码行」是 `cgdbpy/` 去空行去注释后的实测值，含 `cgdb.py` 的 171 行共 3,747。）

**事前估算 vs 事后实际**：动手前的可行性分析估的是 ~4,500 行 /
3–5 周做到日常可用 / 2–3 个月做到功能对等（对照：C 版 Windows 移植走了
M0–M6 七个里程碑）。实际 `cgdbpy/` 是 3,576 行，比估算少 20%。
**估算偏保守的主要是 UI 层**（估 2,500，实际 app.py 1,215）——
因为 ptk 的 Buffer 把行编辑器整块接管了（§3.9）。

缩减的主要来源不是「Python 更简洁」，而是 **三块重量级 C 代码在 Python 里根本不用写**：
gdbwire（412 行替代 5,486 行）、vterm（架构上不需要）、平台层（标准库）。

## 18. 分发与平台约束

### 18.1 分发（Python 版最难看的一项）

| 方式 | 体积 [估算] | 启动 | 备注 |
|---|---:|---|---|
| 要求用户装 Python + `pip install` | ~0 | 260 ms | 对开发者友好，对普通用户不友好 |
| `pipx install` | ~0 | 260 ms | 隔离环境，仍需系统有 Python |
| PyInstaller onefile | 20–30 MB | **更慢**（要解压到临时目录） | 单文件但启动退步 |
| PyInstaller onedir | 25–40 MB | ~260 ms | 启动最好的打包方案 |
| Nuitka | 15–25 MB | 略快 | 编译成 C，构建慢 |

对比 C 版：**6.6 MB 单文件全静态，8.5 ms 启动**。**没有好的解法，只能权衡。**
以上体积/启动数字均为 **[估算]**，未实测 —— 真要打包时先测一遍再写进文档。

### 18.2 平台约束的变化

- **32 位约束消失（实质优势）**：C 版受 mingw32 限制，32 位 gdb 只能调 32 位
  inferior（`win_specs.md` §11.3）。Python 版只是发 MI 命令给 gdb 进程，
  **gdb 是几位的和前端无关**，天然跨位数。
- **CJK 输入**：C 版限制 #4 是「VT 输入模式按控制台码页转换，非 UTF-8 码页下
  CJK 键入会乱」。ptk 在 Windows 上走 `ReadConsoleInputW` 宽字符路径，
  **大概率不受影响，但未实测** —— 要支持 CJK 前先写个 E2E 验证。
- **ConPTY 依赖仍在**：E2E 测试用 ConPTY（Win10 1809+）。但**运行时不依赖** ——
  ptk 直接写控制台，inferior tty 用命名管道（古董级 API，无版本约束）。
  这比 C 版好：C 版的 inferior 终端路线要 ConPTY。

## 19. 什么情况下**不该**做 Python 版

诚实地列出反面理由（写于实现之前，实现之后依然成立）：

1. **如果目标只是「Windows 上能用 cgdb」—— 已经达成了。**
   `windows` 分支的 C 版是全功能原生版，6.6 MB 单文件，8.5 ms 启动，
   E2E + MI driver + Linux 回归全绿。
   Python 版在**每一个性能指标上都更差**（§13.1），且分发更麻烦（§18.1）。
2. **如果目标是给上游 cgdb 贡献**，上游是 C11/C++11 项目，
   Python 重写不可能被合并，等于开一个独立的新项目，要自己长期维护。
3. **如果目标是「更快的开发速度来试新想法」**，那 Python 版有价值 ——
   但更省事的做法是给 C 版加一个 Python 插件接口，或者直接用路线 B
   （gdb 内嵌 Python，§15.2）做原型。

**Python 版真正站得住的理由只有两个**：
- 想要一个**易于二次开发/扩展**的 cgdb（写个新窗格、新命令，Python 比 C 快一个量级）
- 想要**超过 C 版 Windows 移植的功能**（Tab 补全、Ctrl-R 搜索、鼠标、
  更丰富的语法高亮），而这些在 C 里做代价很高

如果这两条不成立，就不该做。§8 的未实现项清单基本就是第一条理由的展开。

---

## 附录 A：基准与探测脚本

⚠️ **这些脚本目前在 `.tmp/pyver/`，被 gitignore，没有入库。**
它们是 §13 全部数字和 §16 选型对比的唯一来源。要复现或重新测量，
需要先把它们从 `.tmp/pyver/` 找回来（或按本文描述重写）。
**如果你要长期维护这份代码，建议把它们挪进 `python/tools/` 并提交。**

| 脚本 | 测什么 | 对应本文 |
|---|---|---|
| `bench_mi.py` | MI 往返延迟（prompt 同步，**有缺陷**，保留作反面证据） | §7.2 注 |
| `bench_mi2.py` | token 同步版 + 大负载尝试 | §13.2 |
| `bench_mi3.py` | **延迟分解**：gdb+pipe vs Python 唤醒；Windows 定时器粒度排查 | §13.2 |
| `bench_bulk.py` | 大负载摄入吞吐（`info functions .` = 593 KB） | §13.3 |
| `bench_pygments.py` | Pygments 全文/窗口 lex 吞吐 + style 查表成本 | §13.4 |
| `bench_window_lex.py` | **窗口 lex 正确性**：11 个偏移 × 40 行 vs 全文 lex 逐行对比 | §3.6 |
| `conpty_bench.py` | ConPTY 基准 harness（复用 `.tmp/conpty_run.py` 的接线配方） | §16 |
| `bench_app_textual.py` | Textual 渲染基准（含 driver 层帧计数） | §16 |
| `bench_app_ptk.py` | prompt_toolkit 渲染基准（含 renderer 层帧计数） | §16 |
| `probe_tty.py` | **inferior-tty 四种拼写实测**（管道自身连通性 + 引号变体矩阵） | §7.2 |
| `probe_breakpoint.py` | **`-break-insert` 13 种 location 拼写实测** | §7.2 |
| `oracle_kui.py` | **kui 黄金参考生成器**（pty 里驱动真实 C `kui_driver`） | §9.2 |
| `target.c` | 被调试的测试程序（`int big[100000]` + `compute()`） | — |

跑法（Git Bash，仓库根目录）：

```bash
export PATH="/d/Dev/mingw32/bin:$PATH"        # gdb.exe 和被调试程序需要
cd .tmp/pyver
PY=/c/Share/Python311/python.exe              # ⚠️ 不要用 mingw32 自带的 python

$PY bench_mi3.py                              # 延迟分解
$PY bench_bulk.py                             # 大负载吞吐
$PY bench_pygments.py                         # 高亮吞吐
$PY bench_window_lex.py                       # 窗口 lex 正确性
$PY probe_tty.py                              # inferior-tty 拼写矩阵
$PY probe_breakpoint.py                       # breakpoint location 拼写矩阵
$PY conpty_bench.py bench_app_ptk.py     5000 out_ptk.json
$PY conpty_bench.py bench_app_textual.py 5000 out_textual.json
BENCH_CAPTURE=vt.bin $PY conpty_bench.py bench_app_textual.py 500 out_t.json  # dump VT
```

注意事项：
- ConPTY 尺寸由 `BENCH_ROWS` / `BENCH_COLS` 控制，默认 40×110，和 C 版 E2E 一致。
- `bench_mi.py` 的 prompt 同步方式会给出 ~100 倍偏高的延迟，**别拿它的数字做决策**
  （这正是它被保留的原因：说明为什么必须用 token 配对）。
- `oracle_kui.py` 必须在 **WSL2** 里跑（需要 Linux 的 `kui_driver` 和 pty），
  且 `settle` 窗口必须 > 1000 ms（kui 的映射超时），否则输出会串用例。
  详见 §9.2。

## 附录 B：本机 Python 生态现状

| 包 | 状态 | 用途 |
|---|---|---|
| `prompt_toolkit` 3.0.52 | ✅ 已装 | **TUI 框架**（§16 选定） |
| `Pygments` 2.20.0 | ✅ 已装 | 语法高亮，替代 `lib/tokenizer` |
| `wcwidth` 0.2.5 | ✅ 已装 | ptk 的唯一依赖 |
| `rich` 14.3.3 | ✅ 已装 | Textual 的依赖；本项目未直接使用 |
| `textual` 8.2.3 | ✅ 已装 | 落选的 TUI（§16），基准对照用 |
| `tree_sitter` | ✅ 已装（核心） | Textual `TextArea` 增量高亮所需 |
| `tree_sitter_c` / `_cpp` | ❌ 未装 | **Textual 的 `syntax` extra 也不含这两个**（§16.1 第 6 点） |
| `pygdbmi` | ❌ 未装 | §15.4 决定自写，不需要 |
| `pyte` | ❌ 未装 | 终端模拟，仅 §8.8 全屏 inferior 才需要 |
| `urwid` | ❌ 未装 | 落选 TUI，未实测 |
| `pexpect` | ❌ 未装 | 路线 A 用不到（直接 `subprocess` + 管道） |
| `pywinpty` | ❌ 未装 | ConPTY 路线需要；本项目手写 ctypes，无依赖 |
| `pytest` 9.0.3 | ✅ 已装 | 测试框架 |

Python 3.11.9 @ `C:\Share\Python311\python.exe`。
另有 mingw32 自带的 Python 3.14.3 @ `D:\Dev\mingw32\bin\python.exe`
（**未用于本文任何测量**，跑基准时注意别选错）。

**运行期只需要 `prompt_toolkit` + `pygments`**（`wcwidth` 随 ptk 装）。
其余都是测试或落选方案的依赖。

## 附录 C：可以直接复用的仓库资产

这是「Python 版比 C 移植更容易」的核心论据 —— 不是 Python 更好写，
而是**这个仓库里已经躺着大量可直接搬的东西**。下表右列是实际采用情况。

| 资产 | 位置 | 实际怎么用上的 |
|---|---|---|
| MI-over-pipes 传输设计 | `doc/win_specs.md` §7 | ✅ 管道接线、EOF 判定照搬；两个致命陷阱中「同步管道读写死锁」直接决定了 `inferior_tty.py` 用重叠 I/O |
| 合成 GDB 窗口规格 | `doc/win_specs.md` §8.1 | ✅ 行模型字节语义表逐条实现，`test_console.py` 54 项对着它写 |
| 内嵌行编辑器键位表 | `doc/win_specs.md` §8.2 | ✅ 但**没有照抄实现** —— ptk 的 Buffer 免费提供，C 版那 405 行不用写（§3.9） |
| MI 命令映射表 | `doc/reference.md` §5.2 | ✅ `gdb_run/next/step/finish/until/up/down` 直接对应 |
| 四条核心数据流 | `doc/reference.md` §1.4 | ✅ 本文 §2.3 就是它的 Python 版翻译 |
| **kui 的 198 项行为规格** | `testsuite/kui.base/basic.exp` | ✅ **翻译成 pytest 成了 keymap 引擎的验收标准**（§9.2），这是最值钱的一笔 |
| kui 三张静态表 | `lib/kui/kui_term.cpp` | ✅ `keycodes.py` 逐条移植（92 项记法 + 47 项硬编码序列） |
| E2E 场景矩阵 | `doc/win_specs.md` §10.2 + `.tmp/e2e_cgdb.py` | ✅ `tests/e2e/test_e2e.py` 的 22 项场景对着它设计 |
| ConPTY 接线配方 | `doc/porting-feasibility.md` §3.1 + `.tmp/conpty_run.py` | ✅ `tests/e2e/conpty.py` 直接复用（三个坑都躲过了） |
| `:set` 选项清单 | `cgdb/cgdbrc.cpp` | ✅ `cgdbrc.py` 23 项逐条照抄，且测试反向抓取 C 源码防漂移（§3.7） |
| 已知限制清单 | `doc/win_specs.md` §11 | ✅ 逐条评估：#2（无补全）→ §8.1 待做；#3（32 位）→ 已消除；#7（全屏 inferior）→ §8.8 决定不做 |

**特别值得强调 `.tmp/e2e_cgdb.py` + `.tmp/conpty_run.py`**：
这是一套已经调通的「在 ConPTY 里跑真程序 + VT 屏幕解析 + 断言」框架。
Python 版**用同一套方法测自己**（`tests/e2e/conpty.py` 是它的重写版），
所以两边的验收标准可以直接对齐。§16 的 TUI 基准也是复用这套接线跑的。

## 附录 D：参考

- [gdbgui — How it Works](https://www.gdbgui.com/howitworks/)：
  路线 A 的先例，明确说明用 pygdbmi 管理 gdb 子进程并解析输出
- [cs01/pygdbmi](https://github.com/cs01/pygdbmi)：MI 解析库（§15.4 评估后未采用）
- [cyrus-and/gdb-dashboard](https://github.com/cyrus-and/gdb-dashboard)：
  路线 B 的先例，gdb 内嵌 Python API
- [GDB Front Ends（GDB Wiki）](https://sourceware.org/gdb/wiki/GDB%20Front%20Ends)：
  现有前端全景，含 gdb 自带 TUI（路线 C）
- [Textualize/textual](https://github.com/textualize/textual)：落选的 TUI 框架（§16）
- GDB 手册 "GDB/MI output syntax" —— `mi.py` 实现的那个文法的权威定义

## 附录 E：文件清单

```
python/
├── cgdb.py                     203   入口、argv 解析、rc 加载、-w 等待附加
├── README.md                   264   用户文档 + 踩坑清单（本文的精简版）
├── .gitignore                        __pycache__ / *.pyc / .pytest_cache
├── cgdbpy/
│   ├── __init__.py               5
│   ├── mi.py                   412   MI 记录解析
│   ├── gdbclient.py            387   gdb 子进程 + 三线程 + 请求队列
│   ├── inferior_tty.py         276   命名管道(重叠IO) / pty
│   ├── kui.py                  407   前缀树 + 双超时 + 递归展开
│   ├── keycodes.py             326   键值常量 + 三张记法表
│   ├── console.py              251   GDB 窗行模型 + 滚动状态
│   ├── source.py               361   源码缓冲 + 高亮缓存 + 断点表
│   ├── highlight.py            128   Pygments token → ptk 样式
│   ├── cgdbrc.py               222   23 个 :set 选项
│   └── app.py                 1215   ptk 界面、焦点、按键分发、gdb 动作
└── tests/
    ├── conftest.py               8   把 python/ 加进 sys.path
    ├── data/kui_oracle.json   1106   C kui_driver 的黄金输出（198 用例）
    ├── test_kui.py             536   359 项
    ├── test_app.py             837    82 项
    ├── test_mi.py              310    72 项
    ├── test_console.py         401    54 项
    ├── test_cgdbrc.py          298    54 项
    ├── test_source.py          648    46 项
    ├── test_gdb_integration.py 755    25 项（真 gdb）
    └── e2e/
        ├── conpty.py           384   ConPTY 会话 + VT100 屏幕模型
        └── test_e2e.py         589    22 项（真 cgdb + 真 gdb + 真 ConPTY）
```
