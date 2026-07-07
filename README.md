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

A general-purpose MCP server around the `liggghts` binary. Instead of
hand-writing shell commands and tracking background processes yourself, you
describe the simulation task and the agent can call these generic tools:

| Tool | Purpose |
|------|---------|
| `validate_liggghts_bin(run_parse_probe, run_runtime_probe, mpi_ranks, mpirun)` | Check the configured `LIGGGHTS_BIN`, optional MPI launch path, safety limits, and optional self-contained `mesh/surface/stress` capability probes. |
| `estimate_case_cost(input_script, num_procs)` | Statically lint a deck, flag risky commands, and report rough run-size hints. |
| `validate_deck(input_script, execute, timeout, num_procs, mpirun)` | Statically validate a deck and optionally execute a tiny smoke/probe deck in a tempdir. |
| `create_dem_case(case_type, name, particle_count, ...)` | Generate structured starter decks for angle-of-repose, box-settling, and simple silo-discharge workflows, optionally writing or launching the case. |
| `create_advanced_dem_template(template_type, name, particle_count, ...)` | Generate advanced scaffolds for inclined chute, rotating drum, direct shear cell, and hopper-flow workflows. |
| `validate_case_assets(run_id, case_dir, deck_relpath, ...)` | Check a case deck, referenced files, missing assets, file hashes, and STL/OBJ mesh bounds. |
| `ensure_standard_outputs(input_script, run_id, write_back, ...)` | Add common thermo, custom dump, and optional VTK dump commands before the first run/minimize command. |
| `start_simulation(input_script, num_procs, name, overwrite, mpirun)` | Launch a run from inline LIGGGHTS deck text. |
| `start_parameter_sweep(template, parameters, name_prefix, num_procs, overwrite, mpirun)` | Render `{{param}}` deck templates and launch one run per parameter set. |
| `start_calibration_sweep(template, parameters, targets, ...)` | Launch a parameter sweep with calibration target metadata attached. |
| `start_from_file(input_path, num_procs, name, overwrite, mpirun)` | Launch a run from an existing input deck file. |
| `clone_run(run_id, new_name, replacements, append_text, deck_relpath, num_procs, overwrite, mpirun)` | Copy an existing run, edit its deck, and launch the clone. |
| `resume_from_restart(source_run_id, restart_relpath, new_name, continuation_script, run_steps, ...)` | Build and optionally launch a continuation case from a restart file. |
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode, overwrite, mpirun)` | Snapshot a whole case directory, then run a deck inside that isolated snapshot. |
| `prepare_batch(cases, batch_id, overwrite, validate)` | Prepare queued run directories from inline decks, templates, standard cases, or advanced scaffolds. |
| `start_batch(batch_id, max_start, num_procs, mpirun)` | Start queued cases from a prepared batch while respecting concurrency limits. |
| `batch_status(batch_id)` | Summarize queued/running/finished states for a prepared batch. |
| `list_batches()` | List prepared batch manifests. |
| `check_status(run_id)` | Return running/finished/unknown status, exit code, and the last log line. |
| `read_log(run_id, tail)` | Read the tail of `log.run`. |
| `list_outputs(run_id, patterns, include_bookkeeping)` | List dumps, trajectories, CSV/JSON summaries, logs, VTK files, restart files, and other matching outputs. |
| `list_output_details(run_id, patterns, include_bookkeeping)` | List outputs with relative path, kind, size, and modified timestamp. |
| `list_dumps(run_id)` | Convenience wrapper for dump/VTK/restart/post files. |
| `read_output(run_id, relpath, offset, max_bytes)` | Safely read a bounded text window from a run output file. |
| `parse_log(run_id, max_bytes)` | Extract warnings, errors, final thermo row, and loop summary from `log.run`. |
| `analyze_dump_metrics(run_id, input_relpath, max_frames, ...)` | Parse LIGGGHTS/LAMMPS custom dumps and export frame metrics as JSON/CSV. |
| `summarize_run(run_id, max_outputs)` | Combine status, parsed log, and output metadata for one run. |
| `compare_runs(run_ids)` | Compare compact summaries for multiple runs. |
| `evaluate_calibration(run_ids, targets, analyze_dumps, ...)` | Score runs against target metrics from summaries and dump metrics. |
| `suggest_calibration_cases(best_parameters, parameter_bounds, ...)` | Suggest next-round parameter cases around the current best calibration point. |
| `validate_visualization_tools()` | Check OVITO Python, OVITO GUI, ParaView, `pvpython`, and `pvbatch` availability. |
| `convert_dump_with_ovito(run_id, input_relpath, output_relpath, export_format)` | Convert dumps/trajectories through OVITO into VTK/XYZ/LAMMPS-dump style outputs. |
| `render_with_ovito(run_id, input_relpath, output_relpath)` | Render a PNG screenshot from a dump/trajectory through OVITO. |
| `render_with_paraview(run_id, input_relpath, output_relpath)` | Render a PNG screenshot from VTK/VTP/VTU/CSV data through ParaView. |
| `collect_run_artifacts(run_id, patterns, artifact_dir, mode)` | Collect simulation and visualization outputs into `artifacts/` with a manifest. |
| `summarize_visual_outputs(run_id)` | List generated screenshots, animations, converted data, and visualization files. |
| `generate_run_report(run_id, include_dump_metrics, ...)` | Generate reproducible JSON and Markdown reports with status, logs, assets, metrics, and visualization outputs. |
| `list_runs()` | List known run directories and their persisted status. |
| `stop_simulation(run_id)` | Terminate a running simulation process group and record that the stop was user-requested. |

Each run lives in its own directory under `~/liggghts_runs/<run_id>/` with
`input.in`, `log.run`, `pid`, `cmd`, `status.json`, `exit_code`, plus whatever
LIGGGHTS writes. The wrapper persists the child exit code, so completed runs
remain inspectable after the MCP server restarts.

The server also exposes MCP resources:

| Resource | Purpose |
|----------|---------|
| `liggghts://config` | Current binary, run root, and safety configuration. |
| `liggghts://runs` | Known runs. |
| `liggghts://runs/{run_id}/status` | One run's status. |
| `liggghts://runs/{run_id}/summary` | One run's status/log/output summary. |
| `liggghts://runs/{run_id}/outputs` | One run's output file metadata. |
| `liggghts://runs/{run_id}/input` | The run input deck. |
| `liggghts://runs/{run_id}/log` | Tail of `log.run`. |

Bundled prompts provide starting points for common workflows:
`angle_of_repose_case`, `silo_discharge_case`, `mesh_wall_probe`, and
`parameter_sweep`.

### Prerequisites

1. **LIGGGHTS binary**. Install one from your package manager or build from
   <https://github.com/CFDEMproject/LIGGGHTS-PUBLIC>.

   The server reads `LIGGGHTS_BIN` and defaults to `/usr/local/bin/liggghts`.

2. **Python 3.10+** with `pip` / `venv`.

3. **MPI** if you want `num_procs > 1`; the server launches
   `mpirun -np N "$LIGGGHTS_BIN" -in input.in`.

4. **Optional visualization tools**:
   - OVITO Python module, available to `python3` or configured with
     `OVITO_PYTHON`.
   - ParaView command-line tools, especially `pvpython` or `pvbatch`, for VTK
     rendering.

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
- "Create a structured `box_settle` starter case with 5k particles, validate
  its assets, then launch it."
- "Create an inclined chute advanced template at 28 degrees and run a small
  smoke case."
- "Launch `~/cases/silo.in`, name it `silo_v1`, and don't wait."
- "Sweep friction coefficient from 0.2 to 0.6 over 5 generic cases."
- "Start a calibration sweep and rank the runs by target solid fraction."
- "Prepare a batch of 12 cases and start only the first 3 now."
- "Resume `silo_v1` from the newest restart for another 50k steps."
- "Is `silo_v1` done yet? Show the last 50 log lines."
- "Add standard dump outputs to this deck, run it, convert the dump with OVITO,
  then render a preview image."
- "Analyze `particles.lammpstrj` into CSV/JSON metrics and generate a final run
  report."
- "Render the VTK outputs from `silo_v1` with ParaView and collect artifacts."

The agent can generate starter cases, validate assets, write or reuse decks,
call `start_simulation`, resume from restarts, poll `check_status`, read logs,
summarize outputs, analyze dumps, convert dumps, render OVITO/ParaView
previews, and generate final reports.

### Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LIGGGHTS_BIN` | `/usr/local/bin/liggghts` | LIGGGHTS executable |
| `LIGGGHTS_RUNS` | `~/liggghts_runs` | Run directory root |
| `LIGGGHTS_READ_ONLY` | `0` | Disable mutating tools such as start/stop/clone/sweep. |
| `LIGGGHTS_ALLOW_OVERWRITE` | `0` | Allow `overwrite=True` to delete an existing finished run dir. |
| `LIGGGHTS_MAX_RANKS` | unset | Maximum MPI ranks accepted by tools/probes. |
| `LIGGGHTS_MAX_CONCURRENT_RUNS` | unset | Maximum simultaneously running cases. |
| `LIGGGHTS_ALLOWED_CASE_ROOTS` | unset | `:`-separated source roots for `start_from_file` / `start_from_dir`. |
| `LIGGGHTS_MAX_SWEEP_CASES` | `32` | Maximum cases created by one parameter sweep. |
| `LIGGGHTS_MAX_BATCH_CASES` | `64` | Maximum cases prepared by one batch manifest. |
| `LIGGGHTS_MAX_READ_BYTES` | `262144` | Maximum bytes returned by output/log readers. |
| `LIGGGHTS_MAX_ANALYSIS_BYTES` | `33554432` | Maximum bytes read from a dump by analysis/report tools. |
| `LIGGGHTS_ALLOW_DECK_SHELL` | `0` | Allow LIGGGHTS deck `shell` commands. Disabled by default. |
| `LIGGGHTS_VALIDATE_TIMEOUT_MAX` | `120` | Maximum timeout accepted by `validate_deck(execute=True)`. |
| `LIGGGHTS_VIS_TIMEOUT` | `300` | Maximum seconds for one conversion/render command. |
| `OVITO_PYTHON` | auto | Python executable that can `import ovito`. |
| `OVITO_BIN` | auto | Optional OVITO GUI executable for capability reporting. |
| `PARAVIEW_BIN` | auto | Optional ParaView GUI executable for capability reporting. |
| `PVPYTHON_BIN` | auto | ParaView Python executable used by `render_with_paraview`. |
| `PVBATCH_BIN` | auto | ParaView batch executable used when `use_pvbatch=True`. |

`LIGGGHTS_BIN` may be an absolute path, a `~/...` path, or a bare executable
name resolvable from the MCP server's `PATH`.

Custom `list_outputs(patterns=...)` glob patterns are intentionally confined to
the selected run directory: absolute patterns and patterns containing `..` are
rejected.

Decks containing LIGGGHTS `shell` commands are rejected by default. Set
`LIGGGHTS_ALLOW_DECK_SHELL=1` only for trusted decks.

### Docker

Build the server image:

```bash
docker build -t liggghts-mcp .
```

Run with host LIGGGHTS and a persistent run directory:

```bash
docker run --rm -i \
  -e LIGGGHTS_BIN=/usr/local/bin/liggghts \
  -v /usr/local/bin/liggghts:/usr/local/bin/liggghts:ro \
  -v "$HOME/liggghts_runs:/root/liggghts_runs" \
  liggghts-mcp
```

### Bundled Libraries

If your `liggghts` binary needs shared libraries that your loader cannot find,
drop those `.so` files into [lib/](lib/) and the server will inject
`LD_LIBRARY_PATH` for the child process. Library files are gitignored.

### Tests

```bash
python -m unittest
```

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

这是一个通用的 `liggghts` MCP server。它不包含个人项目流水线；负责启动、
监控、停止、检查和汇总通用 LIGGGHTS 仿真。

| 工具 | 用途 |
|------|------|
| `validate_liggghts_bin` | 检查 `LIGGGHTS_BIN`、MPI 启动路径，以及可选的 `mesh/surface/stress` 自包含 probe。 |
| `estimate_case_cost` | 静态检查 deck，提示风险命令和大致规模。 |
| `validate_deck` | 静态验证 deck，可选在临时目录执行小型 smoke/probe。 |
| `create_dem_case` | 生成休止角、盒内沉降、简单料仓卸料等结构化 starter case，并可选择写入或启动。 |
| `create_advanced_dem_template` | 生成斜槽、转鼓、直剪盒、料斗流等高级 scaffold。 |
| `validate_case_assets` | 检查 case deck、引用文件、缺失资产、文件哈希和 STL/OBJ 网格边界。 |
| `ensure_standard_outputs` | 在首个 `run` / `minimize` 前补充常用 thermo、dump 和可选 VTK 输出命令。 |
| `start_simulation` | 从 deck 文本启动仿真。 |
| `start_parameter_sweep` | 渲染 `{{param}}` 模板并启动参数扫描。 |
| `start_calibration_sweep` | 启动带目标指标元数据的标定参数扫描。 |
| `start_from_file` | 从已有输入文件启动仿真。 |
| `clone_run` | 复制已有 run、修改 deck 并启动新 run。 |
| `resume_from_restart` | 从已有 restart 文件准备并可选启动续算 run。 |
| `start_from_dir` | 快照整个 case 目录，再在隔离副本里运行指定 deck。 |
| `prepare_batch` | 从 deck、模板、标准 case 或高级 scaffold 准备 queued 批处理 case。 |
| `start_batch` | 按并发限制启动批处理中的 queued case。 |
| `batch_status` | 汇总批处理 case 的 queued/running/finished 状态。 |
| `list_batches` | 列出已准备的批处理 manifest。 |
| `check_status` | 查询运行状态、退出码和最后一行日志。 |
| `read_log` | 读取 `log.run` 末尾。 |
| `list_outputs` | 列出输出文件、日志、dump、restart、CSV/JSON、VTK 等。 |
| `list_output_details` | 列出输出文件的相对路径、类型、大小和修改时间。 |
| `list_dumps` | 只列 dump / VTK / restart / post 输出。 |
| `read_output` | 安全读取 run 目录内输出文件的一段文本。 |
| `parse_log` | 从 `log.run` 提取 warning/error、最后 thermo 行和 loop summary。 |
| `analyze_dump_metrics` | 解析 LIGGGHTS/LAMMPS custom dump，并导出逐帧 JSON/CSV 指标。 |
| `summarize_run` | 汇总单个 run 的状态、日志和输出。 |
| `compare_runs` | 对比多个 run 的摘要。 |
| `evaluate_calibration` | 按目标指标对多个 run 打分排序。 |
| `suggest_calibration_cases` | 围绕当前最优参数建议下一轮标定 case。 |
| `validate_visualization_tools` | 检查 OVITO Python、OVITO、ParaView、`pvpython` 和 `pvbatch`。 |
| `convert_dump_with_ovito` | 使用 OVITO 将 dump/轨迹转换为 VTK/XYZ/其他格式。 |
| `render_with_ovito` | 使用 OVITO 从 dump/轨迹渲染 PNG 预览图。 |
| `render_with_paraview` | 使用 ParaView 从 VTK/VTP/VTU/CSV 渲染 PNG 预览图。 |
| `collect_run_artifacts` | 将仿真和可视化输出收集到 `artifacts/` 并生成 manifest。 |
| `summarize_visual_outputs` | 汇总截图、动画、转换文件和可视化脚本。 |
| `generate_run_report` | 生成包含状态、日志、资产、指标和可视化结果的 JSON/Markdown 报告。 |
| `list_runs` | 列出所有已知 run。 |
| `stop_simulation` | 停止正在运行的仿真，并记录这是用户主动停止。 |

每个 run 位于 `~/liggghts_runs/<run_id>/`，包含 `input.in`、`log.run`、
`pid`、`cmd`、`status.json`、`exit_code` 以及 LIGGGHTS 自己写出的文件。
退出码会持久化，所以 MCP 重启后仍能检查已完成 run。

同时提供 MCP resources：`liggghts://config`、`liggghts://runs`、
`liggghts://runs/{run_id}/status`、`summary`、`outputs`、`input` 和 `log`。
内置 prompts 包括 `angle_of_repose_case`、`silo_discharge_case`、
`mesh_wall_probe` 和 `parameter_sweep`。

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
| `LIGGGHTS_READ_ONLY` | `0` | 禁用启动、停止、clone、sweep 等修改型工具。 |
| `LIGGGHTS_ALLOW_OVERWRITE` | `0` | 允许 `overwrite=True` 删除已有已结束 run 目录。 |
| `LIGGGHTS_MAX_RANKS` | 未设置 | 限制最大 MPI ranks。 |
| `LIGGGHTS_MAX_CONCURRENT_RUNS` | 未设置 | 限制同时运行的 case 数。 |
| `LIGGGHTS_ALLOWED_CASE_ROOTS` | 未设置 | 限制 `start_from_file` / `start_from_dir` 的来源根目录。 |
| `LIGGGHTS_MAX_SWEEP_CASES` | `32` | 单次参数扫描最多 case 数。 |
| `LIGGGHTS_MAX_BATCH_CASES` | `64` | 单个批处理 manifest 最多准备的 case 数。 |
| `LIGGGHTS_MAX_READ_BYTES` | `262144` | 输出/日志读取最多返回字节数。 |
| `LIGGGHTS_MAX_ANALYSIS_BYTES` | `33554432` | 指标分析/报告工具最多读取 dump 的字节数。 |
| `LIGGGHTS_ALLOW_DECK_SHELL` | `0` | 是否允许 deck 中的 `shell` 命令；默认拒绝。 |
| `LIGGGHTS_VALIDATE_TIMEOUT_MAX` | `120` | `validate_deck(execute=True)` 可接受的最大 timeout。 |
| `LIGGGHTS_VIS_TIMEOUT` | `300` | 单个转换/渲染命令的最大秒数。 |
| `OVITO_PYTHON` | 自动 | 能够 `import ovito` 的 Python 可执行文件。 |
| `OVITO_BIN` | 自动 | 可选 OVITO GUI 命令，仅用于能力检查。 |
| `PARAVIEW_BIN` | 自动 | 可选 ParaView GUI 命令，仅用于能力检查。 |
| `PVPYTHON_BIN` | 自动 | `render_with_paraview` 使用的 ParaView Python。 |
| `PVBATCH_BIN` | 自动 | `use_pvbatch=True` 时使用的 ParaView batch 命令。 |

`LIGGGHTS_BIN` 可以是绝对路径、`~/...` 路径，或 MCP server 启动环境
`PATH` 中能找到的可执行文件名。

自定义 `list_outputs(patterns=...)` glob 会被限制在对应 run 目录内；绝对
路径和包含 `..` 的 pattern 会被拒绝。

默认会拒绝包含 LIGGGHTS `shell` 命令的 deck；只有确认 deck 可信时才设置
`LIGGGHTS_ALLOW_DECK_SHELL=1`。

如果 `LIGGGHTS_BIN` 不是默认路径，注册 MCP 时传入：

```bash
claude mcp add -s user liggghts \
  --env LIGGGHTS_BIN=/opt/liggghts/bin/liggghts \
  -- /绝对路径/liggghts-mcp/.venv/bin/python \
     /绝对路径/liggghts-mcp/server.py
```

升级后需要 remove/add MCP，并新开 Claude Code 会话；如果旧的 `server.py`
stdio 子进程还在，先手动 kill 再重新添加。

### 测试

```bash
python -m unittest
```
