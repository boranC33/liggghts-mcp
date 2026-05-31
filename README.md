# liggghts-mcp

> An [MCP](https://modelcontextprotocol.io) server that lets LLM agents drive
> [LIGGGHTS](https://www.cfdem.com/liggghts-open-source-discrete-element-method-particle-simulation-code)
> DEM simulations.
>
> 让 LLM agent 直接启动、监控和管理 LIGGGHTS 颗粒动力学仿真。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## English

### What Is This

A thin MCP wrapper around the `liggghts` binary. Instead of hand-writing shell
commands and tracking background processes yourself, you describe the simulation
task and the agent can call these generic tools:

| Tool | Purpose |
|------|---------|
| `validate_liggghts_bin(run_parse_probe, run_runtime_probe, mpi_ranks, mpirun)` | Check the configured `LIGGGHTS_BIN`, optional MPI launch path, and optional self-contained `mesh/surface/stress` capability probes. |
| `start_simulation(input_script, num_procs, name, overwrite, mpirun)` | Launch a run from inline LIGGGHTS deck text. |
| `start_from_file(input_path, num_procs, name, overwrite, mpirun)` | Launch a run from an existing input deck file. |
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode, overwrite, mpirun)` | Snapshot a whole case directory, then run a deck inside that isolated snapshot. |
| `check_status(run_id)` | Return running/finished/unknown status, exit code, and the last log line. |
| `read_log(run_id, tail)` | Read the tail of `log.run`. |
| `list_outputs(run_id, patterns, include_bookkeeping)` | List dumps, trajectories, CSV/JSON summaries, logs, VTK files, restart files, and other matching outputs. |
| `list_dumps(run_id)` | Convenience wrapper for dump/VTK/restart/post files. |
| `list_runs()` | List known run directories and their persisted status. |
| `stop_simulation(run_id)` | Terminate a running simulation process group and record that the stop was user-requested. |

Each run lives in its own directory under `~/liggghts_runs/<run_id>/` with
`input.in`, `log.run`, `pid`, `cmd`, `status.json`, `exit_code`, plus whatever
LIGGGHTS writes. The wrapper persists the child exit code, so completed runs
remain inspectable after the MCP server restarts.

### Prerequisites

1. **LIGGGHTS binary**. Install one from your package manager or build from
   <https://github.com/CFDEMproject/LIGGGHTS-PUBLIC>.

   The server reads `LIGGGHTS_BIN` and defaults to `/usr/local/bin/liggghts`.

2. **Python 3.10+** with `pip` / `venv`.

3. **MPI** if you want `num_procs > 1`; the server launches
   `mpirun -np N "$LIGGGHTS_BIN" -in input.in`.

### MPI Scheduling

MPI here means multiple LIGGGHTS ranks, not one process automatically using many
threads. For example:

```bash
mpirun -np 4 "$LIGGGHTS_BIN" -in input.in
```

Choose rank counts that fit the host. A conservative first rule is to avoid
requesting more total ranks than available CPU cores unless you intentionally
want oversubscription.

### Install

```bash
git clone https://github.com/boranC33/liggghts-mcp
cd liggghts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Wire It Into Claude Code

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

### Reloading After An Upgrade

After `git pull` or any code change, restart the MCP server and open a fresh
Claude Code session:

```bash
claude mcp remove liggghts -s user
claude mcp add -s user liggghts -- \
  /absolute/path/to/liggghts-mcp/.venv/bin/python \
  /absolute/path/to/liggghts-mcp/server.py
```

If an older stdio child is still alive, kill it before re-adding:

```bash
pgrep -af 'liggghts-mcp.*server.py'
```

### `LIGGGHTS_BIN` Must Be In The MCP Startup Environment

Setting `LIGGGHTS_BIN` only in your interactive shell may not reach the MCP
child process. Either install LIGGGHTS at the default path, or pass the env var
when registering the server:

```bash
claude mcp add -s user liggghts \
  --env LIGGGHTS_BIN=/opt/liggghts/bin/liggghts \
  -- /absolute/path/to/liggghts-mcp/.venv/bin/python \
     /absolute/path/to/liggghts-mcp/server.py
```

If a run fails to launch, call `validate_liggghts_bin()` first. For stricter
mesh-wall capability checks, pass `run_parse_probe=True` and
`run_runtime_probe=True`; both probes use self-contained temporary decks and do
not depend on any external project directory.

### Usage Examples

In a Claude Code session, you can ask:

- "Run an angle-of-repose test with 10k 2 mm particles falling onto a floor."
- "Launch `~/cases/silo.in`, name it `silo_v1`, and don't wait."
- "Sweep friction coefficient from 0.2 to 0.6 over 5 generic cases."
- "Is `silo_v1` done yet? Show the last 50 log lines."

The agent can write or reuse decks, call `start_simulation`, poll
`check_status`, read logs, and summarize outputs.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LIGGGHTS_BIN` | `/usr/local/bin/liggghts` | LIGGGHTS executable |
| `LIGGGHTS_RUNS` | `~/liggghts_runs` | Run directory root |

### Bundled Libraries

If your `liggghts` binary needs shared libraries that your loader cannot find,
drop those `.so` files into [lib/](lib/) and the server will inject
`LD_LIBRARY_PATH` for the child process. Library files are gitignored.

### Known Limitations

- No GPU support is provided by this wrapper; behavior depends on the
  LIGGGHTS binary you point it at.
- `stop_simulation` sends SIGTERM to the process group, then SIGKILL if needed.
  Some MPI launchers may need custom cleanup outside the wrapper.

### License

MIT. LIGGGHTS itself is GPL-2; this wrapper does not link against LIGGGHTS code,
it only spawns the binary as a subprocess.

---

## 中文

### 这是什么

这是一个通用的 `liggghts` MCP 包装器。它不包含个人项目流水线；只负责启动、
监控、停止和检查通用 LIGGGHTS 仿真。

| 工具 | 用途 |
|------|------|
| `validate_liggghts_bin` | 检查 `LIGGGHTS_BIN`、MPI 启动路径，以及可选的 `mesh/surface/stress` 自包含 probe。 |
| `start_simulation` | 从 deck 文本启动仿真。 |
| `start_from_file` | 从已有输入文件启动仿真。 |
| `start_from_dir` | 快照整个 case 目录，再在隔离副本里运行指定 deck。 |
| `check_status` | 查询运行状态、退出码和最后一行日志。 |
| `read_log` | 读取 `log.run` 末尾。 |
| `list_outputs` | 列出输出文件、日志、dump、restart、CSV/JSON、VTK 等。 |
| `list_dumps` | 只列 dump / VTK / restart / post 输出。 |
| `list_runs` | 列出所有已知 run。 |
| `stop_simulation` | 停止正在运行的仿真，并记录这是用户主动停止。 |

每个 run 位于 `~/liggghts_runs/<run_id>/`，包含 `input.in`、`log.run`、
`pid`、`cmd`、`status.json`、`exit_code` 以及 LIGGGHTS 自己写出的文件。
退出码会持久化，所以 MCP 重启后仍能检查已完成 run。

### 安装和接入

```bash
git clone https://github.com/boranC33/liggghts-mcp
cd liggghts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

```bash
claude mcp add -s user liggghts -- \
  /绝对路径/liggghts-mcp/.venv/bin/python \
  /绝对路径/liggghts-mcp/server.py
```

### 环境变量

| 环境变量 | 默认值 | 用途 |
|---------|--------|------|
| `LIGGGHTS_BIN` | `/usr/local/bin/liggghts` | LIGGGHTS 可执行文件 |
| `LIGGGHTS_RUNS` | `~/liggghts_runs` | run 目录根路径 |

如果 `LIGGGHTS_BIN` 不是默认路径，注册 MCP 时传入：

```bash
claude mcp add -s user liggghts \
  --env LIGGGHTS_BIN=/opt/liggghts/bin/liggghts \
  -- /绝对路径/liggghts-mcp/.venv/bin/python \
     /绝对路径/liggghts-mcp/server.py
```

升级后需要 remove/add MCP，并新开 Claude Code 会话；如果旧的 `server.py`
stdio 子进程还在，先手动 kill 再重新添加。
