# liggghts-mcp

> An [MCP](https://modelcontextprotocol.io) server that lets LLM agents (Claude, etc.) drive [LIGGGHTS](https://www.cfdem.com/liggghts-open-source-discrete-element-method-particle-simulation-code) DEM simulations.
>
> 让 LLM (Claude 等) 直接驱动 LIGGGHTS 颗粒动力学仿真的 MCP 服务器。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## English

### What is this

A thin MCP wrapper around the `liggghts` binary, plus project-aware orchestration tools for PINN plate DEM batches. Instead of hand-writing `.in` decks and managing background processes, you describe what you want in natural language and the agent calls these tools:

| Tool | Purpose |
|------|---------|
| `start_simulation(input_script, num_procs, name, overwrite, mpirun)` | Launch a run from inline deck text. `num_procs > 1` prepends `{mpirun} -np N`, so this is MPI ranks, not one multithreaded process. **`overwrite=False`** by default: a `run_id` whose run dir already exists (even from a finished run) is rejected with a clear error; pass `overwrite=True` to clear it first. A still-running prior run is always rejected regardless of the flag |
| `start_from_file(input_path, num_procs, name, overwrite, mpirun)` | Launch a run from an existing `.in` file. Same MPI and `overwrite` semantics as `start_simulation` |
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode, overwrite, mpirun)` | Snapshot a whole case dir (STL, .lammpstrj, .json, …) into the run dir, then run the deck inside it. `num_procs > 1` prepends `{mpirun} -np N`. **`link_mode` defaults to `"copy"`** — `"symlink"` is only safe for read-only inputs (LIGGGHTS writes follow symlinks back into the source case dir and corrupt it). Same `overwrite` semantics as `start_simulation` |
| `run_plate_case(case_dir, pinn_root, name, postprocess, overwrite, mpi_ranks, mpirun)` | Orchestrate the plate-reference pipeline (`run_one_case.sh` settlement → plate_z0 recompute → intrusion, optional postprocess) on a case dir. `mpi_ranks > 1` sets `LIGGGHTS_LAUNCHER="{mpirun} -np {mpi_ranks}"` for the project runner. Real outputs land **inside `case_dir`**, not the run dir — `status.json.case_dir_outputs` records the path so `list_outputs` can find them. **`postprocess=False`** by default — flip to `True` only when `basename(case_dir)` is registered in the PINN sweep manifest. Same `overwrite` semantics as `start_simulation` |
| `validate_liggghts_bin(run_parse_probe=False, run_runtime_probe=False, mpi_ranks=1, mpirun="mpirun")` | Probe `LIGGGHTS_BIN`: exists / executable / version line / capability flags `mesh_surface` and `mesh_surface_stress`. When `mpi_ranks > 1`, `-help`, parse probe, and runtime probe are launched through `{mpirun} -np {mpi_ranks}` to catch MPI/socket/launcher failures before broad runs. When `run_parse_probe=True`, additionally execute a minimal self-contained deck (2-triangle STL + single `fix mesh/surface/stress ... stress on` line, no atoms, no `run`) inside a tempdir and report `mesh_surface_stress_parse_probe`, `parse_probe_exit_code`, `parse_probe_work_dir`, `parse_probe_error_tail`. When `run_runtime_probe=True`, additionally execute an independent runtime deck with granular material properties, `pair_style gran`, `fix wall/gran ... mesh`, one seed atom + `nve/sphere`, and `run 0`. The plate workflow REQUIRES both probes to pass — always probe before assuming a build can or can't run plate cases |
| `check_status(run_id)` | running / finished + exit code + last log line (survives MCP restarts via `status.json`) |
| `read_log(run_id, tail=200)` | Read the tail of `log.run` |
| `list_outputs(run_id, patterns, include_bookkeeping)` | List `*.dump`, `*.lammpstrj`, `*.csv`, `*.json`, `*.log`, `log.*`, `screen.*`, `*.vtk`, `*.vtp`, `*.restart`, `post/*` (configurable). Bookkeeping files (`pid`, `cmd`, `status.json`, `log.run`, `wrapper.sh`, `input.in`, `exit_code`) are excluded by default; pass `include_bookkeeping=True` to surface them — useful for reading `status.json` / `exit_code` directly when self-debugging |
| `list_dumps(run_id)` | Thin alias: dump/vtk/restart/post only |
| `list_runs()` | Status of every known run (reads `status.json`) |
| `stop_simulation(run_id)` | SIGTERM the run (SIGKILL if it ignores). Records `terminated_by_mcp: true` and `termination_signal: "SIGTERM"\|"SIGKILL"` in `status.json` so postprocess can distinguish a user-requested stop from a simulation crash |
| `validate_project_environment(pinn_root, require_serial_liggghts, run_parse_probe, run_runtime_probe, mpi_ranks, mpirun)` | Validate a PINN project root before broad DEM work: required scripts, environment file, LIGGGHTS probes, run-root write access, serial/MPI metadata, CPU-only metadata, and `gpu_used=false` |
| `run_tilt_geometry_gates(pinn_root, tilts_deg, workers, overwrite)` | Run available geometry/contact gates before tilted plate batches. If true per-tilt gates are not available in the PINN checkout, unsupported tilts are reported as failed so broad DEM does not start silently |
| `run_plate_manifest_batch(pinn_root, manifest_path, batch_id, workers, require_geometry_gate, raw_dump_policy, overwrite, resume, mpi_ranks, mpirun)` | Read an explicit project manifest, generate each listed case with `--case-id`, run cases through a real `workers`-wide concurrent worker pool, and pass `LIGGGHTS_LAUNCHER="{mpirun} -np {mpi_ranks}"` when `mpi_ranks > 1`. It persists `status.json`, `batch_metadata.json`, `case_status.csv`, per-case logs, `exit_code`, and `log.run`. Per-case generation/run failures are recorded and remaining cases continue; preflight, geometry gate, raw dump policy, and reference/training promotion failures stop before DEM. It preserves `is_reference_quality=false` and `is_training_data=false` and never uses `--all` |
| `check_batch_status(batch_id, tail)` | Read persistent batch status and case counts after MCP restarts |
| `list_batch_cases(batch_id)` | Return the per-case rows from `case_status.csv` |
| `resume_plate_manifest_batch(batch_id, failed_only, workers, mpi_ranks, mpirun)` | Resume a manifest batch without rerunning completed exit-code-0 cases. Defaults to the previous batch's worker/MPI settings unless overridden |
| `postprocess_plate_batch(pinn_root, batch_id, bins, include_tilted_smoke_postprocess)` | Run project plate postprocessing and report completed, passed, failed, error, dangerous-build, force CSV, and contact-count output counts |
| `package_compact_return(pinn_root, batch_id, phase, include_linux_done, forbid_raw_dumps)` | Create `outputs/transfer/<phase>_windows_return_<timestamp>.tar.gz` and `.sha256`, verify forbidden raw DEM dumps are absent, and write MCP/safety metadata |

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

### MPI and CPU scheduling

MPI in this server means multiple LIGGGHTS ranks, not "one process using many threads". For plain decks, `num_procs=4` launches:

```bash
mpirun -np 4 "$LIGGGHTS_BIN" -in input.in
```

For PINN plate cases, `mpi_ranks=4` sets:

```bash
LIGGGHTS_LAUNCHER="mpirun -np 4"
```

`run_one_case.sh` then uses that launcher for each LIGGGHTS settlement/intrusion step. To use a 16-core CPU-only host efficiently, choose either many serial cases (`workers=16, mpi_ranks=1`) or fewer MPI cases (`workers=4, mpi_ranks=4`). As a first rule, keep `workers * mpi_ranks <= $(nproc)` unless you intentionally want oversubscription.

For large plate sweeps, prefer an MPI-capable LIGGGHTS build without optional graphics dependencies:

```bash
cd /path/to/LIGGGHTS-PUBLIC/src
make mpi_novtk -j"$(nproc)"
export LIGGGHTS_BIN=/path/to/LIGGGHTS-PUBLIC/src/lmp_mpi_novtk
```

Then validate the actual launcher path before a broad run:

```python
validate_liggghts_bin(
    run_parse_probe=True,
    run_runtime_probe=True,
    mpi_ranks=2,
    mpirun="mpirun",
)
```

If the only available binary is a system package such as `/usr/bin/liggghts` and the MPI probe fails with socket/permission errors, stop and fix the launcher/build instead of forcing a broad MPI run.

### Field-tested RFT runbook

For the two-plate RFT equivalent-sphere depth/spacing sweep workflow, see
[docs/operations/rft_equivalent_sphere_depth_spacing.md](docs/operations/rft_equivalent_sphere_depth_spacing.md).
It records the tested 3-concurrent-case / 32-MPI-rank operating mode, sparse
visual dumps, dense force output, file-based monitoring, and the safety rule
for updating MCP without interrupting long-running LIGGGHTS jobs.

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

Open a new Claude Code session and the tools become available.

### Reloading after an upgrade

After `git pull` (or any code change), Claude Code's MCP client keeps the old server alive. To pick up new tools:

```bash
claude mcp remove liggghts -s user
claude mcp add -s user liggghts -- \
  /absolute/path/to/liggghts-mcp/.venv/bin/python \
  /absolute/path/to/liggghts-mcp/server.py
# then start a fresh `claude` session — existing sessions hold the old tool list
```

**Watch for stale stdio children.** `claude mcp remove`/`add` updates `.claude.json` but does **not** kill the existing stdio child process — the old `server.py` keeps serving until its PID is killed. After editing `server.py`, run:

```bash
pgrep -af 'liggghts-mcp.*server.py'
```

If you see a process started before your last edit, kill it manually (`kill <PID>`) before re-adding. Otherwise you'll keep getting v(N-1) behavior even though the file on disk is v(N).

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

If a run fails to launch, call `validate_liggghts_bin()` first — it reports the resolved path, executability, version line, and two capability flags: `mesh_surface` (any `mesh/surface*` style is registered) and `mesh_surface_stress` (grep against `liggghts -help`). The help-grep flag is unreliable in both directions: some apt builds of LIGGGHTS-PUBLIC 3.8.0 register `mesh/surface/stress` even though `-help` omits it, and other builds advertise styles the parser later rejects. Always confirm with the parse probe before deciding whether a binary can run plate cases.

For a stricter check, pass `run_parse_probe=True`. The server creates a tempdir with a 2-triangle STL and a minimal deck containing **only** a `fix mesh/surface/stress ... stress on` line (no atoms, no pair_style, no `run`). If the fix-style constructor succeeds the deck exits 0; if the style isn't registered the deck exits non-zero with "Invalid fix style" in the tail. The deck is bare on purpose — unrelated parse errors (atom sorting, missing materials, etc.) cannot mask the capability signal. The tempdir is left on disk under `/tmp/liggghts_parse_probe_*` for debugging; check `parse_probe_error_tail` first if `mesh_surface_stress_parse_probe` is `false`. Treat `mesh_surface_stress_parse_probe` as the authoritative gate for the plate workflow — the help-grep `mesh_surface_stress` flag is just a fast hint and disagrees with reality on some builds.

For an even stricter check that actually constructs the granular contact model and wires up a mesh wall, pass `run_runtime_probe=True`. This executes an INDEPENDENT self-contained deck inside `/tmp/liggghts_runtime_probe_*`: 2-type `create_box`, full `property/global` material property matrix (Young / Poisson / restitution / friction), `pair_style gran model hertz tangential history`, `fix mesh/surface/stress ... type 2 stress on`, `fix wall/gran ... mesh n_meshes 1 meshes cad`, one seed atom + `nve/sphere` (LIGGGHTS refuses `run 0` with zero atoms), `timestep 1e-6`, `run 0`. The two probes are orthogonal — parse probe answers "is `mesh/surface/stress` registered as a fix style?", runtime probe answers "does this build actually run the granular contact model + `wall/gran mesh` together at `run 0`?". On apt LIGGGHTS-PUBLIC 3.8.0, parse probe sometimes passes while a missing keyword in the contact model could still break a real plate case; running both probes pinpoints which layer fails. Returns `runtime_probe_ok`, `runtime_probe_exit_code`, `runtime_probe_work_dir`, `runtime_probe_error_tail`. The probe deck is self-contained — no project files, no PINN dir.

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

一个轻量 MCP 服务器，把 `liggghts` 命令行和 PINN plate DEM 项目批处理包装成工具，让 LLM agent (Claude Code 等) 直接驱动颗粒动力学仿真。你用自然语言描述要做什么，Agent 自动写 deck、启动、监控、读结果。

| 工具 | 用途 |
|------|------|
| `start_simulation(input_script, num_procs, name, overwrite, mpirun)` | 直接传 deck 文本，后台启动。`num_procs > 1` 会前置 `{mpirun} -np N`，这里的并行是 MPI rank，不是一个进程吃多个线程。**`overwrite=False`** 默认：`run_id` 对应目录已存在（即使是已完成的 run）会直接报错；传 `overwrite=True` 才会清掉重来。还在运行的旧 run 永远拒绝，跟 flag 无关 |
| `start_from_file(input_path, num_procs, name, overwrite, mpirun)` | 从已有 `.in` 文件启动。MPI 和 `overwrite` 语义同 `start_simulation` |
| `start_from_dir(case_dir, deck_relpath, name, num_procs, link_mode, overwrite, mpirun)` | 把整个 case 目录（STL、.lammpstrj、.json 等）快照到 run dir 再跑 deck。`num_procs > 1` 会前置 `{mpirun} -np N`。**`link_mode` 默认 `"copy"`**；`"symlink"` 仅在输入只读时安全（LIGGGHTS 写入会沿 symlink 回写到原 case dir 把源文件损坏）。`overwrite` 语义同 `start_simulation` |
| `run_plate_case(case_dir, pinn_root, name, postprocess, overwrite, mpi_ranks, mpirun)` | 一键跑完 plate-reference 流水线（`run_one_case.sh` settlement → plate_z0 重算 → intrusion，可选 postprocess）。`mpi_ranks > 1` 会给项目 runner 设置 `LIGGGHTS_LAUNCHER="{mpirun} -np {mpi_ranks}"`。真实输出落在 **`case_dir` 内部**，不在 run dir，`status.json.case_dir_outputs` 记录该路径，`list_outputs` 据此能同时列出。**`postprocess=False`** 默认 —— 只有当 `basename(case_dir)` 已注册在 PINN sweep manifest 里时才能传 `True`。`overwrite` 语义同 `start_simulation` |
| `validate_liggghts_bin(run_parse_probe=False, run_runtime_probe=False, mpi_ranks=1, mpirun="mpirun")` | 探测 `LIGGGHTS_BIN`：是否存在 / 可执行 / 版本行 / 两个能力标志 `mesh_surface` 和 `mesh_surface_stress`。`mpi_ranks > 1` 时，`-help`、parse probe、runtime probe 都会通过 `{mpirun} -np {mpi_ranks}` 启动，用来在大批量运行前捕捉 MPI/socket/launcher 问题。`run_parse_probe=True` 时在临时目录额外跑一个极简自包含 deck（2 三角片 STL + 单行 `fix mesh/surface/stress ... stress on`，没有 atoms，没有 `run`），返回 `mesh_surface_stress_parse_probe`、`parse_probe_exit_code`、`parse_probe_work_dir`、`parse_probe_error_tail`。`run_runtime_probe=True` 时再额外跑一个独立 runtime deck，实际构造 granular 接触模型、`wall/gran ... mesh`、单粒子 + `nve/sphere` 和 `run 0`。plate 流水线**两个 probe 都要 pass** —— 断言某个 build 能不能跑 plate 之前永远先 probe |
| `check_status(run_id)` | running / finished + 退出码 + 最后一行日志（通过 `status.json` 跨 MCP 重启保留） |
| `read_log(run_id, tail=200)` | 看 `log.run` 末尾 |
| `list_outputs(run_id, patterns, include_bookkeeping)` | 列出 `*.dump`、`*.lammpstrj`、`*.csv`、`*.json`、`*.log`、`log.*`、`screen.*`、`*.vtk`、`*.vtp`、`*.restart`、`post/*`（可自定义模式）。bookkeeping 文件（`pid`、`cmd`、`status.json`、`log.run`、`wrapper.sh`、`input.in`、`exit_code`）默认排除；传 `include_bookkeeping=True` 才会一起列出 —— 适合 agent 自我调试时直接读 `status.json` / `exit_code` |
| `list_dumps(run_id)` | 薄封装：仅 dump / vtk / restart / post |
| `list_runs()` | 所有 case 的状态总览（读 `status.json`） |
| `stop_simulation(run_id)` | SIGTERM (失败再 SIGKILL)。会在 `status.json` 写入 `terminated_by_mcp: true` 和 `termination_signal: "SIGTERM"\|"SIGKILL"`，让后处理能区分"用户主动停"和"仿真崩了" |
| `validate_project_environment(pinn_root, require_serial_liggghts, run_parse_probe, run_runtime_probe, mpi_ranks, mpirun)` | 在大批量 DEM 前验证 PINN 项目根目录、必需脚本、environment 文件、LIGGGHTS probes、run root 写权限、串行/MPI 元数据、CPU-only 元数据，并明确 `gpu_used=false` |
| `run_tilt_geometry_gates(pinn_root, tilts_deg, workers, overwrite)` | 在 tilted plate 批处理前运行可用的几何/接触 gate；如果当前 PINN checkout 没有真正的逐倾角 gate，会把不支持的 tilt 标成 failed，避免静默启动大批 DEM |
| `run_plate_manifest_batch(pinn_root, manifest_path, batch_id, workers, require_geometry_gate, raw_dump_policy, overwrite, resume, mpi_ranks, mpirun)` | 读取显式 manifest，逐个 `--case-id` 生成 case，然后用真正的 `workers` 并发 worker 池跑项目 runner；`mpi_ranks > 1` 时传入 `LIGGGHTS_LAUNCHER="{mpirun} -np {mpi_ranks}"`。持久化 `status.json`、`batch_metadata.json`、`case_status.csv`、逐 case 日志、`exit_code`、`log.run`。单个 case 生成/运行失败会记录状态并继续剩余 case；preflight、geometry gate、raw dump policy、reference/training promotion 这类硬错误会在 DEM 前停止。保留 `is_reference_quality=false` / `is_training_data=false`，永不使用 `--all` |
| `check_batch_status(batch_id, tail)` | MCP 重启后读取持久化 batch 状态和 case 计数 |
| `list_batch_cases(batch_id)` | 返回 `case_status.csv` 中的逐 case 状态 |
| `resume_plate_manifest_batch(batch_id, failed_only, workers, mpi_ranks, mpirun)` | 继续 manifest batch，不重跑已完成且 exit-code-0 的 case；默认沿用原 batch 的 workers/MPI 设置，也可以显式覆盖 |
| `postprocess_plate_batch(pinn_root, batch_id, bins, include_tilted_smoke_postprocess)` | 运行项目 plate 后处理，报告 completed / passed / failed / error / dangerous-build / force CSV / contact-count 输出数量 |
| `package_compact_return(pinn_root, batch_id, phase, include_linux_done, forbid_raw_dumps)` | 创建 `outputs/transfer/<phase>_windows_return_<timestamp>.tar.gz` 和 `.sha256`，验证 raw DEM dump 未进入包，并写入 MCP/safety metadata |

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

### MPI 和 CPU 调度

这里的 MPI 是多个 LIGGGHTS rank，不是"一个进程自动吃满多个线程"。普通 deck 里，`num_procs=4` 会启动:

```bash
mpirun -np 4 "$LIGGGHTS_BIN" -in input.in
```

PINN plate case 里，`mpi_ranks=4` 会设置:

```bash
LIGGGHTS_LAUNCHER="mpirun -np 4"
```

然后 `run_one_case.sh` 在 settlement / intrusion 两步里用这个 launcher。16 核 CPU-only 主机上，可以选择多串行 case（`workers=16, mpi_ranks=1`），也可以选择少量 MPI case（`workers=4, mpi_ranks=4`）。第一条规则是让 `workers * mpi_ranks <= $(nproc)`，除非你明确想 oversubscribe。

大 plate sweep 推荐用不带可视化依赖的 MPI LIGGGHTS:

```bash
cd /path/to/LIGGGHTS-PUBLIC/src
make mpi_novtk -j"$(nproc)"
export LIGGGHTS_BIN=/path/to/LIGGGHTS-PUBLIC/src/lmp_mpi_novtk
```

大批量 DEM 前先验证真实 launcher:

```python
validate_liggghts_bin(
    run_parse_probe=True,
    run_runtime_probe=True,
    mpi_ranks=2,
    mpirun="mpirun",
)
```

如果只能找到 `/usr/bin/liggghts` 这类系统包，并且 MPI probe 报 socket/permission 错误，应该先修 launcher 或重编译，不要硬上大批量 MPI。

### 已验证的 RFT 运行手册

两板 RFT 等效球 depth/spacing sweep 的实际运行经验记录在
[docs/operations/rft_equivalent_sphere_depth_spacing.md](docs/operations/rft_equivalent_sphere_depth_spacing.md)。
其中包括已验证的 `3` 并发 case、每例 `32` MPI rank、稀疏可视化 dump、密集力输出、基于文件的监控方式，以及更新 MCP 时不打断长时间 LIGGGHTS 任务的安全规则。

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

新开一个 Claude Code 会话，工具就可用了。

### 升级后必须重启 MCP

`git pull`（或修改代码）之后，Claude Code 仍持有旧的 MCP server 实例，新工具不会出现。重新加载步骤：

```bash
claude mcp remove liggghts -s user
claude mcp add -s user liggghts -- \
  /绝对路径/liggghts-mcp/.venv/bin/python \
  /绝对路径/liggghts-mcp/server.py
# 然后必须新开一个 claude 会话；已有会话仍持有旧的工具列表
```

**注意残留的 stdio 子进程。** `claude mcp remove`/`add` 只更新 `.claude.json`，**不会**杀掉已经在跑的 stdio 子进程 —— 老的 `server.py` 还在为客户端服务，直到 PID 被手动杀掉。改完 `server.py` 之后跑：

```bash
pgrep -af 'liggghts-mcp.*server.py'
```

如果看到一个 PID 比你最近一次编辑还早，先 `kill <PID>` 再 re-add。否则磁盘上是 v(N)，行为还是 v(N-1)。

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

仿真起不来时先调一次 `validate_liggghts_bin()` —— 它会返回真正解析到的路径、可执行性、版本行、以及两个能力标志：`mesh_surface`（注册了任何 `mesh/surface*` 风格命令）和 `mesh_surface_stress`（grep `liggghts -help`）。help-grep 这个标志在两个方向都不可靠：部分 apt LIGGGHTS-PUBLIC 3.8.0 实际注册了 `mesh/surface/stress` 但 `-help` 不列；其它 build 又会列出 parser 实际拒绝的 style。要判断某个二进制能不能跑 plate case，一定要用 parse probe 确认，不要只看 help-grep 标志。

如果某些 build 的 `-help` 文本不可信（漏列或错列 `mesh/surface/stress`），传 `run_parse_probe=True` 做更严格的检查：server 会在临时目录写一个 2 三角片 STL 和一个**只**包含一行 `fix mesh/surface/stress ... stress on` 的极简 deck（没有 atoms、没有 pair_style、没有 `run`）。如果 fix-style 构造器成功，deck 以 0 退出；如果该 style 没注册，deck 以非 0 退出，tail 里会有 "Invalid fix style"。deck 故意写得最小 —— 不相关的 parse 错误（atom sort、缺材料属性等）不会再掩盖能力信号。整个 probe 是自包含的 —— 不依赖 PINN 目录或项目 STL。临时目录会保留在 `/tmp/liggghts_parse_probe_*` 下方便 debug；`mesh_surface_stress_parse_probe=false` 时先看 `parse_probe_error_tail`。plate 流水线只信 `mesh_surface_stress_parse_probe`，help-grep 的 `mesh_surface_stress` 只是个快速提示，部分 build 上会跟实际能力对不上。

要做更严格的检查 —— 实际把 granular 接触模型构造起来、把 mesh wall 接进去 —— 传 `run_runtime_probe=True`。这会在 `/tmp/liggghts_runtime_probe_*` 下跑一个**独立**的自包含 deck：2 类 `create_box`、完整的 `property/global` 材料属性矩阵（Young / Poisson / restitution / friction）、`pair_style gran model hertz tangential history`、`fix mesh/surface/stress ... type 2 stress on`、`fix wall/gran ... mesh n_meshes 1 meshes cad`、单 seed 粒子 + `nve/sphere`（LIGGGHTS 在零原子下拒绝 `run 0`）、`neigh_modify delay 0`、`timestep 1e-6`、`run 0`。两个 probe 完全正交 —— parse probe 回答"`mesh/surface/stress` 这个 fix style 注册了吗？"，runtime probe 回答"这个 build 在 `run 0` 时能不能真的把 granular 接触模型 + `wall/gran mesh` 一起跑起来？"。在 apt LIGGGHTS-PUBLIC 3.8.0 上有时 parse probe pass 但接触模型缺一个 keyword 还是会让真实 plate case 挂；两个都跑能精确定位哪一层挂。返回 `runtime_probe_ok`、`runtime_probe_exit_code`、`runtime_probe_work_dir`、`runtime_probe_error_tail`。probe deck 是自包含的 —— 不依赖任何项目文件或 PINN 目录。

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
