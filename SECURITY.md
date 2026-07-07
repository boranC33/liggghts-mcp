# Security Policy

`liggghts-mcp` runs local simulation binaries on behalf of an MCP client. Treat
it like a controlled job launcher, not a sandbox.

## Defaults

- Mutating tools can be disabled with `LIGGGHTS_READ_ONLY=1`.
- Existing run directories cannot be overwritten unless
  `LIGGGHTS_ALLOW_OVERWRITE=1` is set.
- Deck `shell` commands are rejected unless `LIGGGHTS_ALLOW_DECK_SHELL=1` is
  set.
- `start_from_file` and `start_from_dir` can be scoped with
  `LIGGGHTS_ALLOWED_CASE_ROOTS`.
- MPI ranks, concurrent runs, sweep case counts, batch case counts, output read sizes, deck
  validation timeout, dump analysis sizes, and visualization command timeout
  can be bounded with the environment variables documented in `README.md`.
- OVITO/ParaView commands execute local binaries and Python modules. Configure
  `OVITO_PYTHON`, `PVPYTHON_BIN`, and `PVBATCH_BIN` to trusted executables.

## Recommended Production Settings

```bash
LIGGGHTS_READ_ONLY=0
LIGGGHTS_ALLOW_OVERWRITE=0
LIGGGHTS_ALLOW_DECK_SHELL=0
LIGGGHTS_MAX_RANKS=8
LIGGGHTS_MAX_CONCURRENT_RUNS=2
LIGGGHTS_MAX_SWEEP_CASES=16
LIGGGHTS_MAX_BATCH_CASES=32
LIGGGHTS_MAX_READ_BYTES=262144
LIGGGHTS_MAX_ANALYSIS_BYTES=33554432
LIGGGHTS_VIS_TIMEOUT=300
LIGGGHTS_ALLOWED_CASE_ROOTS=/srv/liggghts-cases
OVITO_PYTHON=/usr/bin/python3
PVPYTHON_BIN=/usr/bin/pvpython
PVBATCH_BIN=/usr/bin/pvbatch
```

Use a dedicated OS user for the MCP server and point `LIGGGHTS_RUNS` at a
directory owned by that user.

## Reporting

Please open a private issue or contact the maintainer before publishing a
vulnerability that allows path escape, arbitrary command execution, or unwanted
deletion of run data.
