# Changelog

## 0.8.0

- Added advanced DEM scaffolds via `create_advanced_dem_template` for
  inclined chute, rotating drum, direct shear cell, and hopper-flow workflows.
- Added calibration workflow tools:
  `start_calibration_sweep`, `evaluate_calibration`, and
  `suggest_calibration_cases`.
- Added batch scheduling tools:
  `prepare_batch`, `start_batch`, `batch_status`, and `list_batches`.
- Added `LIGGGHTS_MAX_BATCH_CASES` to bound prepared batch size.

## 0.7.1

- Fixed the generated `silo_discharge` starter deck so primitive `zcylinder`
  walls include the required cylinder-axis coordinates.
- Added regression coverage for the `silo_discharge` wall syntax.

## 0.7.0

- Added structured case generation via `create_dem_case` for
  angle-of-repose, box-settling, and simple silo-discharge starter workflows.
- Added case asset validation via `validate_case_assets`, including deck
  reference checks, file hashes, and STL/OBJ mesh bounds.
- Added restart continuation workflow via `resume_from_restart`.
- Added dump metric extraction via `analyze_dump_metrics`, with JSON/CSV
  exports for frame-level particle bounds, centroid, velocity, radius, solid
  volume, and approximate solid fraction.
- Added reproducible run reports via `generate_run_report`.
- Added `LIGGGHTS_MAX_ANALYSIS_BYTES` to bound dump reads during analysis.

## 0.6.0

- Added deck output augmentation via `ensure_standard_outputs`.
- Added visualization tool discovery via `validate_visualization_tools`.
- Added OVITO conversion and rendering tools:
  `convert_dump_with_ovito` and `render_with_ovito`.
- Added ParaView rendering via `render_with_paraview`.
- Added artifact collection and visualization summaries via
  `collect_run_artifacts` and `summarize_visual_outputs`.
- Added visualization-related output patterns, file classification, helper
  script generation, and command timeouts.
- Documented OVITO/ParaView environment variables and security considerations.

## 0.5.0

- Added MCP resources for config, runs, per-run status, summaries, outputs,
  input decks, and logs.
- Added MCP prompts for angle of repose, silo discharge, mesh-wall probes, and
  parameter sweeps.
- Added safety controls:
  `LIGGGHTS_READ_ONLY`, `LIGGGHTS_ALLOW_OVERWRITE`, `LIGGGHTS_MAX_RANKS`,
  `LIGGGHTS_MAX_CONCURRENT_RUNS`, `LIGGGHTS_ALLOWED_CASE_ROOTS`,
  `LIGGGHTS_MAX_SWEEP_CASES`, `LIGGGHTS_MAX_READ_BYTES`,
  `LIGGGHTS_ALLOW_DECK_SHELL`, and `LIGGGHTS_VALIDATE_TIMEOUT_MAX`.
- Added deck linting and optional tempdir execution via `validate_deck`.
- Added static deck estimation via `estimate_case_cost`.
- Added parameter-sweep launching via `start_parameter_sweep`.
- Added run cloning with deck edits via `clone_run`.
- Added bounded output reading, output metadata, log parsing, run summaries, and
  run comparison tools.
- Hardened output path handling against `..`, absolute paths, and symlink
  escapes.
- Improved process cleanup for completed background runs.
- Added stdlib unit tests, GitHub Actions CI, Dockerfile, and security policy.

## 0.4.1

- Added `PATH`, `~/...`, and relative-path handling for `LIGGGHTS_BIN`.
- Hardened `list_outputs` patterns.
- Fixed `read_log(tail=0)` and negative tail handling.
- Improved SIGTERM/SIGKILL stop status recording.

## 0.4.0

- Kept the server generic by removing project-specific workflows.
- Preserved core run launch, status, log, output listing, MPI, and capability
  probe tools.
