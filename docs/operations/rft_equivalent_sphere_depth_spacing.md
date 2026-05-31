# RFT Equivalent-Sphere Depth-Spacing Sweep Runbook

This note records the field-tested operating pattern from the RFT two-plate
equivalent-sphere LIGGGHTS study. It is not a generic benchmark; it is a
practical runbook for keeping long CPU-only DEM sweeps reproducible and
observable while MCP/server code is updated independently.

## Tested Case Family

The active study varies two parameters:

| Variable | Values |
|---|---|
| Intrusion depth | `10, 20, 30, 40, 50, 60 mm` |
| Plate center spacing | `10, 20, 30, 40, 50, 60, 70, 80 mm` |
| Total cases | `48` |

The deck protocol is:

| Time window | Action |
|---|---|
| `0-1 s` | Particle generation |
| `1-2 s` | Bed settling |
| `2-3 s` | Plate intrusion to the requested depth |
| `3-5 s` | Plate shear in `+X` |
| `5 s` | Stop |

The medium-simplified model keeps the LIGGGHTS granular material/contact
settings but replaces multi-sphere templates with equal-volume spheres. This
is intended for force-ratio trend studies, not final reference-quality data.

## Recommended Runtime Settings

Use the local MPI build, not the system `liggghts` package:

```bash
export LIGGGHTS_BIN=/home/neu/boran/DEM/liggghts-public/src/lmp_mpi
export MPIRUN_BIN=/home/neu/anaconda3/bin/mpirun
export OMP_NUM_THREADS=1
export CUDA_VISIBLE_DEVICES=""
```

For this workload, the stable setting was:

```bash
RFT_MAX_CONCURRENT_CASES=3
RFT_MPI_RANKS=32
```

That runs three cases concurrently, each with 32 MPI ranks. On the tested
96-core host this kept the node fully occupied without the scaling collapse
seen when one case tried to use all ranks.

## Output Schedule

Use sparse visualization dumps and dense force output:

| Output | Time window | Interval | Steps at `dt=4e-6` | Expected count |
|---|---|---:|---:|---:|
| `particles_*.dump` | `0-5 s` | `1.0 s` | `250000` | about `6` files |
| `plate_forces.csv` | `3-5 s` only | `0.01 s` | `2500` | `200` data rows plus header |

This keeps per-case disk use near a few hundred MB instead of multi-GB while
preserving enough force resolution for the front/behind plate ratio.

## Supervisor Pattern

Long RFT sweeps should be supervised by shell scripts that are independent of
the MCP server process. That makes it safe to update or restart MCP without
interrupting active DEM jobs.

The useful file pattern is:

```text
depth_spacing_batch_run/batch.pid
depth_spacing_batch_run/batch.log
depth_spacing_batch_run/status_events.csv
depth_spacing_batch_run/latest_status.txt
depth_spacing_force_summary.csv
```

The batch runner should:

- start at most `RFT_MAX_CONCURRENT_CASES` active cases;
- launch each case through `mpirun -np $RFT_MPI_RANKS`;
- write a per-case `production.pid`;
- log hourly snapshots with step, simulation time, atom count, force rows,
  dump count, case size, disk free, and error markers;
- mark a case complete only when `shadow_replicate_5s.restart` exists and the
  force CSV has the expected row count;
- immediately start the next pending case after a completed case exits.

When handing off from one sweep to another, use a small watcher script that
waits for the previous batch PID to exit, validates summaries and error
markers, and only then starts the next batch. This avoids oversubscribing the
host.

## Monitoring Rules

Prefer file-based status over process listing when inside a sandboxed agent
session. `ps` and `pgrep` can be namespace-sensitive, while LIGGGHTS screen
logs and CSV outputs are authoritative.

Useful checks:

```bash
tail -n 80 depth_spacing_batch_run/batch.log
cat depth_spacing_batch_run/latest_status.txt
tail -n 30 post_depth_030mm_spacing_020mm_equiv_spheres_full/screen.production.log
df -h /home/neu/boran/RFT/liggghts_shadow_replicate
rg -n 'ERROR|Lost atoms|nan' post_depth_*mm_spacing_*mm_equiv_spheres_full/screen.production.log
```

An empty or header-only `plate_forces.csv` is normal before the run reaches
the `3-5 s` shear window. Treat it as `running_partial`, not as failure.

## Observed Throughput

On the tested 96-core CPU host:

| Metric | Observed value |
|---|---:|
| Per-case runtime | about `5.4-6.6 h` |
| Average after first 15 cases | about `5.8 h/case` |
| Parallelism | `3` cases x `32` MPI ranks |
| Per-case disk after completion | about `260 MB` |

For a 48-case matrix, this implies roughly three days wall time, assuming the
deeper intrusion cases do not slow substantially.

## MCP Update Safety

When a long RFT batch is running:

- do not restart or kill active LIGGGHTS/MPI processes unless explicitly asked;
- do not reuse the same output directories for a new test;
- it is safe to edit or update `liggghts-mcp` code if the RFT supervisor is an
  independent shell process;
- after MCP code updates, restart the MCP server only after confirming that no
  active run depends on that MCP child process.

This separation was used successfully: the RFT batch continued running while
the MCP repository was inspected and updated.
