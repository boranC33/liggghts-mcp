# Phase 14B MCP Execution Profile

This profile describes how an upgraded LIGGGHTS-MCP should execute the Phase
14B universal angular planar pilot.

## Inputs

```text
pinn_root=/home/neu/boran/PINN
manifest_path=outputs/plate_reference/liggghts_plate_phase14A_universal_angular_pilot_manifest.csv
batch_id=phase14B_universal_angular_planar_pilot
workers=48
```

## Required tool sequence

```text
validate_project_environment(pinn_root="/home/neu/boran/PINN", require_serial_liggghts=true)
run_tilt_geometry_gates(
  pinn_root="/home/neu/boran/PINN",
  tilts_deg=[0, 5, 10, 15, 18.75, 22.5, 26.25, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180],
  workers=1
)
run_plate_manifest_batch(
  pinn_root="/home/neu/boran/PINN",
  manifest_path="outputs/plate_reference/liggghts_plate_phase14A_universal_angular_pilot_manifest.csv",
  batch_id="phase14B_universal_angular_planar_pilot",
  workers=48,
  require_geometry_gate=true,
  raw_dump_policy="do_not_package",
  overwrite=false
)
check_batch_status(batch_id="phase14B_universal_angular_planar_pilot")
postprocess_plate_batch(
  pinn_root="/home/neu/boran/PINN",
  batch_id="phase14B_universal_angular_planar_pilot",
  bins=64,
  include_tilted_smoke_postprocess=true
)
package_compact_return(
  pinn_root="/home/neu/boran/PINN",
  batch_id="phase14B_universal_angular_planar_pilot",
  phase="phase14B",
  include_linux_done=true,
  forbid_raw_dumps=true
)
```

## Expected return metadata

```text
geometry_gate_passed=18
geometry_gate_total=18
manifest_rows=198
attempted_cases=198
gpu_used=false
raw_dumps_packaged=false
is_reference_quality=false
is_training_data=false
mcp_used=true
scope_note=planar tilt pilot, not full universal 3D closure
```

## Hard stops

Stop the batch if:

- any geometry gate fails;
- LIGGGHTS binary validation fails;
- manifest row count is not `198`;
- raw dump files enter the return tarball;
- any output metadata tries to set `is_reference_quality=true` or `is_training_data=true`;
- GPU is visible as used for DEM.

