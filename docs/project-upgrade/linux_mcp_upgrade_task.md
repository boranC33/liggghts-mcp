# Linux Task: Update LIGGGHTS-MCP for Project Batches

Target host: `neu-NF5280-M7-A0-R0-00`.

Target MCP source:

```text
/home/neu/boran/liggghts-mcp/server.py
```

If the MCP repo is still under `/home/boran/liggghts-mcp`, adapt the path but
preserve the same tool contract.

## Files to sync first

```text
outputs/mcp/liggghts_mcp_project_upgrade/
outputs/plate_reference/liggghts_plate_phase14A_universal_angular_pilot_manifest.csv
outputs/pinn_analysis/phase14A_universal_angular_domain_dem_design/
codex_tasks/linux_todo.md
```

## Required implementation

Implement or extend these MCP tools:

```text
validate_project_environment
run_tilt_geometry_gates
run_plate_manifest_batch
check_batch_status
resume_plate_manifest_batch
postprocess_plate_batch
package_compact_return
```

Keep existing tools such as `validate_liggghts_bin`, `run_plate_case`,
`check_status`, `read_log`, `list_outputs`, and `list_runs` for debugging.

## Verification

Before running Phase 14B, verify:

```bash
claude mcp list
grep -n '@mcp.tool()' /home/neu/boran/liggghts-mcp/server.py
```

Then call the new environment and geometry-gate tools. Do not start the 198-case
DEM batch until all 18 geometry gates pass.

## Return to Windows

After MCP upgrade and smoke validation, append a result to:

```text
codex_tasks/linux_done.md
```

Include:

- MCP server version or git commit;
- tool list;
- environment validation result;
- geometry gate result;
- whether Phase 14B was run through MCP or deferred;
- any blocker.

If Phase 14B is run, return:

```text
outputs/transfer/phase14B_windows_return_<timestamp>.tar.gz
outputs/transfer/phase14B_windows_return_<timestamp>.tar.gz.sha256
```

