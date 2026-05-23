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
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode)` | Snapshot a whole case dir (STL, .lammpstrj, .json, …) into the run dir, then run the deck inside it. **`link_mode` defaults to `"copy"`** — `"symlink"` is only safe for read-only inputs (LIGGGHTS writes follow symlinks back into the source case dir and corrupt it) |
| `run_plate_case(case_dir, pinn_root, name, postprocess)` | Orchestrate the plate-reference pipeline (`run_one_case.sh` settlement → plate_z0 recompute → intrusion, optional postprocess) on a case dir. Real outputs land **inside `case_dir`**, not the run dir — `status.json.case_dir_outputs` records the path so `list_outputs` can find them |
| `validate_liggghts_bin()` | Probe `LIGGGHTS_BIN`: exists / executable / version line / capability flags `mesh_surface` and `mesh_surface_stress`. The plate workflow REQUIRES `mesh_surface_stress=true`; the apt LIGGGHTS package has `mesh/surface` but **not** `mesh/surface/stress` |
| `check_status(run_id)` | running / finished + exit code + last log line (survives MCP restarts via `status.json`) |
| `read_log(run_id, tail=200)` | Read the tail of `log.run` |
| `list_outputs(run_id, patterns)` | List `*.dump`, `*.lammpstrj`, `*.csv`, `*.json`, `*.log`, `log.*`, `screen.*`, `*.vtk`, `*.vtp`, `*.restart`, `post/*` (configurable) |
| `list_dumps(run_id)` | Thin alias: dump/vtk/restart/post only |
| `list_runs()` | Status of every known run (reads `status.json`) |
| `stop_simulation(run_id)` | SIGTERM the run (SIGKILL if it ignores) |

Each run lives in its own directory under `~/liggghts_runs/<run_id>/` with `input.in`, `log.run`, `pid`, `status.json`, plus whatever LIGGGHTS writes (`log.liggghts`, dumps, restart files). The launch wrapper writes the child process's exit code to `<run_dir>/exit_code` after the command finishes, and `status.json` records `started_at`, `finished_at`, `exit_code`, `kind`, and `case_dir_outputs` (for `run_plate_case`) — so a finished run keeps its exit code across MCP server restarts even if `Popen.poll()` is no longer available.

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

### Reloading after an upgrade

After `git pull` (or any code change), Claude Code's MCP client keeps the old server alive. To pick up new tools:

```bash
claude mcp remove liggghts -s user
claude mcp add -s user liggghts -- \
  /absolute/path/to/liggghts-mcp/.venv/bin/python \
  /absolute/path/to/liggghts-mcp/server.py
# then start a fresh `claude` session — existing sessions hold the old tool list
```

### LIGGGHTS_BIN must be set in the MCP startup environment

`LIGGGHTS_BIN` is read at server startup. Setting it only in your interactive shell does **not** propagate to the MCP child process — Claude Code launches the server from its own environment. Either:

- Install LIGGGHTS at the default path (`/usr/local/bin/liggghts`), or
- Pass the env var when registering the server:
  ```bash
  claude mcp add -s user liggghts \
    --env LIGGGHTS_BIN=/opt/liggghts/bin/liggghts \
    -- /absolute/path/to/liggghts-mcp/.venv/bin/python \
       /absolute/path/to/liggghts-mcp/server.py
  ```

If a run fails to launch, call `validate_liggghts_bin()` first — it reports the resolved path, executability, version line, and two capability flags: `mesh_surface` (any `mesh/surface*` style is registered) and `mesh_surface_stress` (the plate workflow requires this — apt LIGGGHTS-PUBLIC 3.8.0 ships `mesh/surface` but is missing `mesh/surface/stress`, so source build is required for plate cases).

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
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode)` | 把整个 case 目录（STL、.lammpstrj、.json 等）快照到 run dir 再跑 deck。**`link_mode` 默认 `"copy"`**；`"symlink"` 仅在输入只读时安全（LIGGGHTS 写入会沿 symlink 回写到原 case dir 把源文件损坏） |
| `run_plate_case(case_dir, pinn_root, name, postprocess)` | 一键跑完 plate-reference 流水线（`run_one_case.sh` settlement → plate_z0 重算 → intrusion，可选 postprocess）。真实输出落在 **`case_dir` 内部**，不在 run dir，`status.json.case_dir_outputs` 记录该路径，`list_outputs` 据此能同时列出 |
| `validate_liggghts_bin()` | 探测 `LIGGGHTS_BIN`：是否存在 / 可执行 / 版本行 / 两个能力标志 `mesh_surface` 和 `mesh_surface_stress`。plate 流水线必须 `mesh_surface_stress=true`；apt LIGGGHTS 只有 `mesh/surface` 没有 `mesh/surface/stress` |
| `check_status(run_id)` | running / finished + 退出码 + 最后一行日志（通过 `status.json` 跨 MCP 重启保留） |
| `read_log(run_id, tail=200)` | 看 `log.run` 末尾 |
| `list_outputs(run_id, patterns)` | 列出 `*.dump`、`*.lammpstrj`、`*.csv`、`*.json`、`*.log`、`log.*`、`screen.*`、`*.vtk`、`*.vtp`、`*.restart`、`post/*`（可自定义模式） |
| `list_dumps(run_id)` | 薄封装：仅 dump / vtk / restart / post |
| `list_runs()` | 所有 case 的状态总览（读 `status.json`） |
| `stop_simulation(run_id)` | SIGTERM (失败再 SIGKILL) |

每个 run 独立目录在 `~/liggghts_runs/<run_id>/`，里面有 `input.in`、`log.run`、`pid`、`status.json`、以及 LIGGGHTS 自己写的 `log.liggghts` 和 dump 文件。启动 wrapper 在子进程结束时把退出码写入 `<run_dir>/exit_code`，`status.json` 记录 `started_at` / `finished_at` / `exit_code` / `kind` / `case_dir_outputs`（`run_plate_case` 才有），即使 MCP server 重启导致 `Popen.poll()` 不可用，已完成 run 的退出码也不会丢。

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

### 升级后必须重启 MCP

`git pull`（或修改代码）之后，Claude Code 仍持有旧的 MCP server 实例，新工具不会出现。重新加载步骤：

```bash
claude mcp remove liggghts -s user
claude mcp add -s user liggghts -- \
  /绝对路径/liggghts-mcp/.venv/bin/python \
  /绝对路径/liggghts-mcp/server.py
# 然后必须新开一个 claude 会话；已有会话仍持有旧的工具列表
```

### `LIGGGHTS_BIN` 要写到 MCP 启动环境里

`LIGGGHTS_BIN` 是在 server 启动时读取的。光在你交互 shell 里 `export` 不会传给 MCP 子进程 —— Claude Code 用它自己的环境启动 server。要么:

- 把 LIGGGHTS 装到默认路径（`/usr/local/bin/liggghts`），要么
- 注册时把环境变量传进去:
  ```bash
  claude mcp add -s user liggghts \
    --env LIGGGHTS_BIN=/opt/liggghts/bin/liggghts \
    -- /绝对路径/liggghts-mcp/.venv/bin/python \
       /绝对路径/liggghts-mcp/server.py
  ```

仿真起不来时先调一次 `validate_liggghts_bin()` —— 它会返回真正解析到的路径、可执行性、版本行、以及两个能力标志：`mesh_surface`（注册了任何 `mesh/surface*` 风格命令）和 `mesh_surface_stress`（plate 流水线必须为 `true`；apt LIGGGHTS-PUBLIC 3.8.0 只有 `mesh/surface`，没有 `mesh/surface/stress`，所以 plate case 必须源码 build）。

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
