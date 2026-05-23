# liggghts-mcp

> An [MCP](https://modelcontextprotocol.io) server that lets LLM agents (Claude, etc.) drive [LIGGGHTS](https://www.cfdem.com/liggghts-open-source-discrete-element-method-particle-simulation-code) DEM simulations.
>
> 让 LLM (Claude 等) 直接驱动 LIGGGHTS 颗粒动力学仿真的 MCP 服务器。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## English

### What is this

A thin MCP wrapper around the `liggghts` binary. Instead of hand-writing `.in` decks and managing background processes, you describe what you want in natural language and the agent calls these tools:

| Tool | Purpose |
|------|---------|
| `start_simulation(input_script, num_procs, name)` | Launch a run from inline deck text |
| `start_from_file(input_path, num_procs, name)` | Launch a run from an existing `.in` file |
| `check_status(run_id)` | running / finished + exit code + last log line |
| `read_log(run_id, tail=200)` | Read the tail of `log.run` |
| `list_dumps(run_id)` | List `*.dump`, `*.vtk`, `post/*` outputs |
| `list_runs()` | Status of every known run |
| `stop_simulation(run_id)` | SIGTERM the run (SIGKILL if it ignores) |

Each run lives in its own directory under `~/liggghts_runs/<run_id>/` with `input.in`, `log.run`, `pid`, plus whatever LIGGGHTS writes (`log.liggghts`, dumps, restart files).

### Prerequisites

1. **LIGGGHTS binary**. On Debian/Ubuntu:
   ```bash
   sudo apt install liggghts
   ```
   Or build from source: <https://github.com/CFDEMproject/LIGGGHTS-PUBLIC>.

   The server reads `LIGGGHTS_BIN` (default `/usr/local/bin/liggghts`).

2. **Python 3.10+** with `pip` / `venv`.

3. (Optional) **MPI** if you want `num_procs > 1` — server invokes `mpirun -np N`.

### Install

```bash
git clone https://github.com/boranC33/liggghts-mcp
cd liggghts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Wire it into Claude Code

```bash
claude mcp add -s user liggghts -- \
  /absolute/path/to/liggghts-mcp/.venv/bin/python \
  /absolute/path/to/liggghts-mcp/server.py
```

Verify:
```bash
claude mcp list
# liggghts: ... - ✓ Connected
```

Open a new Claude Code session and the 7 tools become available.

### Usage examples

In a Claude Code session, just say:

- *"Run an angle-of-repose test: 10k 2 mm particles falling from a funnel onto a plate."*
- *"Launch `~/cases/silo.in`, name it `silo_v1`, don't wait."*
- *"Sweep friction coefficient from 0.2 to 0.6 over 5 cases."*
- *"Is `silo_v1` done yet? Last 50 lines please."*

The agent writes the deck, calls `start_simulation`, polls `check_status`, and summarizes results.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LIGGGHTS_BIN` | `/usr/local/bin/liggghts` | LIGGGHTS executable |
| `LIGGGHTS_RUNS` | `~/liggghts_runs` | Where run directories live |

### Bundled libraries (optional)

If your `liggghts` binary needs `libliggghts.so.3` and your loader can't find it, drop the `.so` files into [lib/](lib/) and the server will inject `LD_LIBRARY_PATH` for the child process. Library files are gitignored — distribute them separately or install the system package.

### Known limitations

- `insert/pack` aborts with no diagnostic in some Debian builds of LIGGGHTS-PUBLIC 3.8.0. Use `lattice + create_atoms` or `insert/stream` instead, or build LIGGGHTS from source.
- No GPU support — depends on whichever LIGGGHTS binary you point at.
- `stop_simulation` sends SIGTERM to the process group; some MPI setups may need a custom kill strategy.

### License

MIT. LIGGGHTS itself is GPL-2; this wrapper does not link against LIGGGHTS code, it only spawns the binary as a subprocess.

---

## 中文

### 这是什么

一个轻量 MCP 服务器，把 `liggghts` 命令行包装成 7 个工具，让 LLM agent (Claude Code 等) 直接驱动颗粒动力学仿真。你用自然语言描述要做什么，Agent 自动写 deck、启动、监控、读结果。

| 工具 | 用途 |
|------|------|
| `start_simulation(input_script, num_procs, name)` | 直接传 deck 文本，后台启动 |
| `start_from_file(input_path, num_procs, name)` | 从已有 `.in` 文件启动 |
| `check_status(run_id)` | running / finished + 退出码 + 最后一行日志 |
| `read_log(run_id, tail=200)` | 看 `log.run` 末尾 |
| `list_dumps(run_id)` | 列出 dump / vtk / post 文件 |
| `list_runs()` | 所有 case 的状态总览 |
| `stop_simulation(run_id)` | SIGTERM (失败再 SIGKILL) |

每个 run 独立目录在 `~/liggghts_runs/<run_id>/`，里面有 `input.in`、`log.run`、`pid`、以及 LIGGGHTS 自己写的 `log.liggghts` 和 dump 文件。

### 先决条件

1. **LIGGGHTS 二进制**。Debian/Ubuntu:
   ```bash
   sudo apt install liggghts
   ```
   或源码 build: <https://github.com/CFDEMproject/LIGGGHTS-PUBLIC>。

   服务器读 `LIGGGHTS_BIN` 环境变量（默认 `/usr/local/bin/liggghts`）。

2. **Python 3.10+** 带 `pip` / `venv`。

3. (可选) **MPI**，要并行就需要 — `num_procs > 1` 时服务器调 `mpirun -np N`。

### 安装

```bash
git clone https://github.com/boranC33/liggghts-mcp
cd liggghts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 接入 Claude Code

```bash
claude mcp add -s user liggghts -- \
  /绝对路径/liggghts-mcp/.venv/bin/python \
  /绝对路径/liggghts-mcp/server.py
```

验证:
```bash
claude mcp list
# liggghts: ... - ✓ Connected
```

新开一个 Claude Code 会话，7 个工具就可用了。

### 使用示例

在 Claude Code 会话里直接说：

- *"用 LIGGGHTS 跑一个堆积角测试：1 万个 2mm 颗粒从漏斗落到平板上"*
- *"把 ~/cases/silo.in 启动，叫它 silo_v1，别等它跑完"*
- *"摩擦系数从 0.2 扫到 0.6 跑 5 个 case"*
- *"silo_v1 跑完没？最后 50 行日志给我"*

Agent 会自动写 deck、调 `start_simulation`、轮询 `check_status`、跑完总结结果。

### 配置

| 环境变量 | 默认值 | 用途 |
|----------|--------|------|
| `LIGGGHTS_BIN` | `/usr/local/bin/liggghts` | LIGGGHTS 可执行文件 |
| `LIGGGHTS_RUNS` | `~/liggghts_runs` | run 目录的根 |

### 自带共享库（可选）

如果你的 `liggghts` 二进制依赖 `libliggghts.so.3` 而系统找不到，把 `.so` 文件放到 [lib/](lib/) 下，服务器会自动给子进程注入 `LD_LIBRARY_PATH`。`lib/` 下的库文件已被 gitignore，单独分发或装系统包即可。

### 已知限制

- 部分 Debian 版本的 LIGGGHTS-PUBLIC 3.8.0 在执行 `insert/pack` 时会无诊断 abort。改用 `lattice + create_atoms` 或 `insert/stream`，或者源码 build。
- 不支持 GPU — 取决于你指向的 LIGGGHTS 二进制。
- `stop_simulation` 给整个进程组发 SIGTERM；某些 MPI 配置可能需要自定义 kill 策略。

### 许可证

MIT。LIGGGHTS 本身是 GPL-2；本 wrapper 不与 LIGGGHTS 代码静态链接，只通过子进程调用它。

---

## Contributing

PRs welcome. Issues and feature requests at <https://github.com/boranC33/liggghts-mcp/issues>.
