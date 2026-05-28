# LIGGGHTS-MCP Project Upgrade Plan

Status: design ready for Linux-side MCP update.

This plan upgrades LIGGGHTS-MCP from a single-case runner into a project-aware
DEM orchestration layer. It is based on the DEM workflow already exercised in
Phases 7T to 7Y, 8A to 8C, 10F, 12B, 12P, 12Y, and the Phase 14A pilot design.

## Why update MCP

The current project has outgrown a one-case `run_plate_case` interface. The
completed DEM phases repeatedly used the same pattern:

1. validate LIGGGHTS binary and project environment;
2. validate tilted plate geometry before broad execution;
3. generate case folders from a manifest;
4. run CPU-only LIGGGHTS with bounded workers;
5. postprocess force-depth and contact-count summaries;
6. package compact return files;
7. verify that raw dumps are not transferred;
8. preserve safety metadata and non-promotion flags.

MCP should expose that whole pattern directly, while still keeping the existing
single-case tools for debugging.

## Required MCP capabilities

### 1. Project environment validation

New or upgraded tool:

```text
validate_project_environment(pinn_root, require_serial_liggghts=true)
```

It should report:

- `pinn_root`;
- Python version;
- LIGGGHTS binary path;
- whether the binary is serial or MPI-enabled;
- required project scripts present/missing;
- `environment.sh` status;
- write access to run root;
- GPU visibility, with `gpu_used=false` for DEM.

This fixes the recurring uncertainty around `/usr/bin/liggghts` versus
`/home/neu/boran/DEM/liggghts-public/src/lmp_serial`.

### 2. Tilted geometry gate

New tool:

```text
run_tilt_geometry_gates(pinn_root, tilts_deg, workers=1)
```

It should:

- materialize missing tilted STL assets;
- run one-particle contact tests for every requested tilt;
- return pass/fail per tilt;
- stop later DEM batch execution if any required gate fails.

For Phase 14B this must cover:

```text
0, 5, 10, 15, 18.75, 22.5, 26.25, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180
```

### 3. Manifest-driven batch execution

New tool:

```text
run_plate_manifest_batch(
  pinn_root,
  manifest_path,
  batch_id,
  workers,
  require_geometry_gate=true,
  raw_dump_policy="do_not_package",
  overwrite=false
)
```

It should:

- read the project CSV manifest;
- keep `enabled=false` as metadata, not as a blocker when Windows explicitly
  requests the batch;
- generate case folders with existing project scripts;
- run cases with bounded CPU workers;
- write persistent per-case and batch status;
- support resume after interruption;
- preserve `is_reference_quality=false` and `is_training_data=false`.

The tool must not silently run cases outside the provided manifest.

### 4. Persistent status and resume

Upgrade existing status handling:

```text
check_batch_status(batch_id)
list_batch_cases(batch_id)
resume_plate_manifest_batch(batch_id, failed_only=true)
```

Status must survive MCP server restarts. Minimum files:

```text
status.json
case_status.csv
exit_code
batch_metadata.json
```

Each case status row should include:

- `case_id`;
- `status`;
- `exit_code`;
- `settlement_completed`;
- `intrusion_completed`;
- `liggghts_error`;
- `dangerous_builds`;
- `force_csv_exists`;
- `contact_count_csv_exists`;
- `started_at`;
- `finished_at`.

### 5. Project postprocessing

New or upgraded tool:

```text
postprocess_plate_batch(pinn_root, batch_id, bins=64)
```

It should run the same postprocessing chain used by direct shell fallback:

- `postprocess_liggghts_plate_hpc_sweep.py`;
- `postprocess_liggghts_plate_tilted_smoke.py` when tilted cases exist;
- dataset/package builders when explicitly requested by Windows.

It should report:

- completed cases;
- completed stable cases;
- passed cases;
- failed cases;
- LIGGGHTS error cases;
- dangerous-build cases;
- packaged force CSV count;
- packaged contact-count CSV count.

### 6. Compact return packaging

New tool:

```text
package_compact_return(
  pinn_root,
  batch_id,
  phase,
  include_linux_done=true,
  forbid_raw_dumps=true
)
```

It should generate:

```text
outputs/transfer/<phase>_windows_return_<timestamp>.tar.gz
outputs/transfer/<phase>_windows_return_<timestamp>.tar.gz.sha256
```

Forbidden raw dump names:

```text
contact_local.dump
particles.lammpstrj
settlement_particles.lammpstrj
settled_particles.lammpstrj
```

The tool must fail if any forbidden file enters the tarball.

### 7. Safety metadata

Every MCP project batch must explicitly write:

```text
gpu_used=false
raw_dumps_packaged=false
is_reference_quality=false
is_training_data=false
```

For Windows-side provenance, also include:

```text
mcp_used=true
mcp_server_version
liggghts_bin
serial_liggghts=true/false
workers
manifest_rows
attempted_cases
```

## Phase 14B applicability

The upgraded MCP should be able to run Phase 14B as one managed batch:

- manifest rows: `198`;
- geometry gate rows: `18`;
- recommended workers: `48`;
- CPU-only;
- no raw dumps in return package;
- no reference/training promotion;
- explicit note that Phase 14B is a planar pilot, not a full universal 3D closure.

## Compatibility with previous phases

The same MCP batch tools should support older workflows:

- Phase 8B material/seed grid: 27 cases;
- Phase 10F angular force-depth packaging: 79 cases;
- Phase 12B tangential pilot: 57 cases;
- Phase 12P reference-candidate expansion: 174 cases;
- Phase 12Y targeted tilt-holdout correction: 141 cases;
- Phase 14B universal angular planar pilot: 198 cases.

## Non-goals

- Do not make MCP responsible for scientific promotion decisions.
- Do not make MCP classify data as reference quality.
- Do not make MCP train neural models.
- Do not treat the Phase 14B planar tilt sweep as universal 3D closure.

