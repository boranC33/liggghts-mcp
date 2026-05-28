"""LIGGGHTS MCP server — start runs, poll status, read logs, list outputs."""
from mcp.server.fastmcp import FastMCP
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

mcp = FastMCP("liggghts")

__version__ = "0.3.2"

LIGGGHTS_BIN = os.environ.get("LIGGGHTS_BIN", "/usr/local/bin/liggghts")
RUNS = Path(os.environ.get("LIGGGHTS_RUNS", Path.home() / "liggghts_runs"))
RUNS.mkdir(parents=True, exist_ok=True)

_LIB_DIR = Path(__file__).parent / "lib"


def _child_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    if _LIB_DIR.is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            f"{_LIB_DIR}:{existing}" if existing else str(_LIB_DIR)
        )
    if extra:
        env.update(extra)
    return env


def _validate_rank_count(value: int, name: str) -> None:
    if value < 1:
        raise ValueError(f"{name} must be >= 1")


def _mpi_prefix(ranks: int, mpirun: str = "mpirun") -> list[str]:
    _validate_rank_count(ranks, "mpi ranks")
    if ranks == 1:
        return []
    launcher = shlex.split(mpirun.strip()) if mpirun.strip() else ["mpirun"]
    return launcher + ["-np", str(ranks)]


def _mpi_launcher_string(ranks: int, mpirun: str = "mpirun") -> str:
    prefix = _mpi_prefix(ranks, mpirun)
    return shlex.join(prefix) if prefix else ""


def _liggghts_cmd(args: list[str], mpi_ranks: int = 1, mpirun: str = "mpirun") -> list[str]:
    return _mpi_prefix(mpi_ranks, mpirun) + [LIGGGHTS_BIN] + args


def _plate_env(pinn: Path, mpi_ranks: int = 1, mpirun: str = "mpirun") -> dict:
    env = {"LIGGGHTS_BIN": LIGGGHTS_BIN, "PINN_ROOT": str(pinn)}
    launcher = _mpi_launcher_string(mpi_ranks, mpirun)
    if launcher:
        env["LIGGGHTS_LAUNCHER"] = launcher
    return env


def _mpi_status_fields(
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
    *,
    workers: int | None = None,
) -> dict:
    launcher = _mpi_launcher_string(mpi_ranks, mpirun)
    fields = {
        "mpi_used": mpi_ranks > 1,
        "mpi_ranks_per_case": mpi_ranks,
        "mpi_launcher": launcher,
        "mpirun": mpirun,
    }
    if workers is not None:
        fields["total_requested_mpi_ranks"] = workers * mpi_ranks
    return fields


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    wd = (RUNS / run_id).resolve()
    if RUNS.resolve() not in wd.parents and wd != RUNS.resolve():
        raise ValueError("run_id escapes runs root")
    return wd



_PROJECT_REQUIRED_PATHS = (
    "scripts/prepare_intrusion_plate_z0.py",
    "scripts/validate_liggghts_plate_hpc_setup.py",
    "scripts/generate_liggghts_plate_hpc_manifest.py",
    "scripts/generate_liggghts_plate_hpc_cases.py",
    "scripts/postprocess_liggghts_plate_hpc_sweep.py",
    "hpc/liggghts_plate_tier1/run_one_case.sh",
    "hpc/liggghts_plate_tier1/environment.sh",
)

_FORBIDDEN_RAW_DUMPS = {
    "contact_local.dump",
    "particles.lammpstrj",
    "settlement_particles.lammpstrj",
    "settled_particles.lammpstrj",
}

_BATCH_FIELDS = [
    "case_id", "status", "exit_code", "settlement_completed",
    "intrusion_completed", "liggghts_error", "dangerous_builds",
    "force_csv_exists", "contact_count_csv_exists", "started_at", "finished_at",
    "case_dir", "run_id", "case_log",
]


def _batch_dir(batch_id: str) -> Path:
    return _run_dir(batch_id)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _resolve_pinn(pinn_root: str) -> Path:
    pinn = Path(pinn_root).expanduser().resolve()
    if not pinn.is_dir():
        raise FileNotFoundError(f"pinn_root not a directory: {pinn_root}")
    return pinn


def _resolve_manifest(pinn: Path, manifest_path: str) -> Path:
    path = Path(manifest_path).expanduser()
    if not path.is_absolute():
        path = pinn / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    return path


def _read_manifest(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_case_status(batch_dir: Path, rows: list[dict]) -> None:
    with (batch_dir / "case_status.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_BATCH_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in _BATCH_FIELDS})


def _read_case_status(batch_dir: Path) -> list[dict]:
    path = batch_dir / "case_status.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _log_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _dangerous_builds(case_dir: Path) -> int:
    total = 0
    for name in ("settle_screen.log", "screen.log", "log.liggghts"):
        for line in _log_text(case_dir / name).splitlines():
            if "Dangerous builds" not in line:
                continue
            try:
                total += int(line.split("=", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return total


def _liggghts_errors(case_dir: Path) -> str:
    out = []
    for name in ("settle_screen.log", "screen.log", "log.liggghts"):
        for line in _log_text(case_dir / name).splitlines():
            if "ERROR" in line:
                out.append(f"{name}: {line.strip()}")
    return "; ".join(out)


def _case_completed(case_dir: Path, *names: str) -> bool:
    return any("Loop time" in _log_text(case_dir / name) for name in names)


def _case_status_from_dir(case_id: str, case_dir: Path, run_id: str = "") -> dict:
    errors = _liggghts_errors(case_dir)
    return {
        "case_id": case_id,
        "status": "completed" if _case_completed(case_dir, "settle_screen.log") and _case_completed(case_dir, "screen.log", "log.liggghts") and not errors else "failed",
        "exit_code": 0 if not errors else 1,
        "settlement_completed": str(_case_completed(case_dir, "settle_screen.log")).lower(),
        "intrusion_completed": str(_case_completed(case_dir, "screen.log", "log.liggghts")).lower(),
        "liggghts_error": errors,
        "dangerous_builds": _dangerous_builds(case_dir),
        "force_csv_exists": str((case_dir / "plate_force_raw.csv").exists()).lower(),
        "contact_count_csv_exists": str((case_dir / "contact_local.dump").exists()).lower(),
        "started_at": "",
        "finished_at": _now_iso(),
        "case_dir": str(case_dir),
        "run_id": run_id,
    }


def _batch_summary(rows: list[dict]) -> dict:
    completed = [r for r in rows if r.get("status") == "completed"]
    failed = [r for r in rows if r.get("status") == "failed"]
    running = [r for r in rows if r.get("status") == "running"]
    pending = [r for r in rows if r.get("status") in ("pending", "skipped")]
    return {
        "completed_cases": len(completed),
        "failed_cases": len(failed),
        "running_cases": len(running),
        "pending_cases": len(pending),
    }


def _geometry_gate_ready(pinn: Path) -> dict:
    for gate_dir in sorted(RUNS.glob("geometry_gates_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        status = _read_json(gate_dir / "geometry_gate_status.json")
        if status.get("pinn_root") == str(pinn):
            return {
                "gate_id": status.get("gate_id", gate_dir.name),
                "ready_for_dem": bool(status.get("ready_for_dem")),
                "passed_count": status.get("passed_count", 0),
                "failed_count": status.get("failed_count", 0),
                "failed_tilts": status.get("failed_tilts", []),
                "status_path": str(gate_dir / "geometry_gate_status.json"),
            }
    return {
        "gate_id": "",
        "ready_for_dem": False,
        "passed_count": 0,
        "failed_count": None,
        "failed_tilts": [],
        "status_path": "",
    }


def _read_pid(wd: Path) -> int | None:
    f = wd / "pid"
    if not f.exists():
        return None
    try:
        return int(f.read_text().strip())
    except ValueError:
        return None


_PROCS: dict[str, subprocess.Popen] = {}


def _alive(pid: int) -> bool:
    """True iff process exists AND is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line.split(None, 2)[1]
    except FileNotFoundError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_status(wd: Path) -> dict:
    f = wd / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_status(wd: Path, status: dict) -> None:
    (wd / "status.json").write_text(json.dumps(status, indent=2))


def _last_log_line(wd: Path) -> str:
    log = wd / "log.run"
    if not log.exists():
        return ""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _finalize_if_done(run_id: str, wd: Path, status: dict) -> dict:
    """If status says running but the process is gone, persist finished + exit code.

    Prefers the wrapper-written `exit_code` file (survives MCP restarts) over
    `Popen.poll()` (only valid for the current MCP process).

    Returns updated status dict (mutated in place).
    """
    if status.get("status") != "running":
        return status
    pid = status.get("pid")
    file_rc = _read_exit_code_file(wd)
    if file_rc is None:
        # No exit_code file yet. If we have a live Popen handle, ask it.
        # Otherwise, infer aliveness from /proc.
        proc = _PROCS.get(run_id)
        if proc is not None:
            poll_rc = proc.poll()
            if poll_rc is None:
                return status
            rc = poll_rc
        else:
            if pid is not None and _alive(pid):
                return status
            rc = None
    else:
        rc = file_rc
    status["status"] = "finished"
    status["exit_code"] = rc
    status["finished_at"] = _now_iso()
    _write_status(wd, status)
    return status


def _read_exit_code_file(wd: Path) -> int | None:
    """Read the wrapper-persisted exit code, if present and parseable."""
    f = wd / "exit_code"
    if not f.exists():
        return None
    try:
        return int(f.read_text().strip())
    except (ValueError, OSError):
        return None


def _launch(
    wd: Path,
    cmd: list[str],
    *,
    run_id: str,
    extra_status: dict | None = None,
    env_extra: dict | None = None,
    cwd: Path | None = None,
) -> dict:
    """Spawn a background process, write pid/cmd/status.json, register in _PROCS.

    Wraps `cmd` in a bash one-liner that captures the real exit code into
    `<wd>/exit_code` after the child exits, so completion state survives MCP
    server restarts (where in-process Popen handles in `_PROCS` are lost).
    """
    # Stale exit_code from any prior run in this dir would be misread by
    # check_status as the current run's status — clear it before launch.
    ec_file = wd / "exit_code"
    if ec_file.exists():
        try:
            ec_file.unlink()
        except OSError:
            pass

    quoted_cmd = shlex.join(cmd)
    quoted_ec_path = shlex.quote(str(ec_file))
    wrapper = (
        f"set +e; {quoted_cmd}; rc=$?; "
        f'printf "%s\\n" "$rc" > {quoted_ec_path}; exit "$rc"'
    )
    spawn_cmd = ["bash", "-c", wrapper]

    log = open(wd / "log.run", "w")
    proc = subprocess.Popen(
        spawn_cmd,
        cwd=cwd or wd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=_child_env(env_extra),
    )
    (wd / "pid").write_text(str(proc.pid))
    (wd / "cmd").write_text(quoted_cmd)
    _PROCS[run_id] = proc
    status = {
        "run_id": run_id,
        "pid": proc.pid,
        "status": "running",
        "started_at": _now_iso(),
        "cmd": quoted_cmd,
        "work_dir": str(wd),
    }
    if extra_status:
        status.update(extra_status)
    _write_status(wd, status)
    return status


_PARSE_PROBE_STL = """\
solid plate_probe
  facet normal 0.0 0.0 1.0
    outer loop
      vertex -0.05 -0.05 0.0
      vertex  0.05 -0.05 0.0
      vertex  0.05  0.05 0.0
    endloop
  endfacet
  facet normal 0.0 0.0 1.0
    outer loop
      vertex -0.05 -0.05 0.0
      vertex  0.05  0.05 0.0
      vertex -0.05  0.05 0.0
    endloop
  endfacet
endsolid plate_probe
"""


_PARSE_PROBE_DECK = """\
atom_style      granular
units           si
boundary        f f f
region          domain block -0.1 0.1 -0.1 0.1 -0.05 0.15 units box
create_box      1 domain
fix             cad all mesh/surface/stress file plate_probe.stl type 1 stress on
"""


_RUNTIME_PROBE_DECK = """\
units           si
atom_style      granular
boundary        f f f
newton          off
communicate     single vel yes

region          domain block -0.1 0.1 -0.1 0.1 -0.05 0.15 units box
create_box      2 domain

neigh_modify    delay 0

fix             m1 all property/global youngsModulus peratomtype 5.0e6 5.0e6
fix             m2 all property/global poissonsRatio peratomtype 0.45 0.45
fix             m3 all property/global coefficientRestitution peratomtypepair 2 0.3 0.3 0.3 0.3
fix             m4 all property/global coefficientFriction peratomtypepair 2 0.4 0.4 0.4 0.4

pair_style      gran model hertz tangential history
pair_coeff      * *

fix             cad all mesh/surface/stress file plate_probe.stl type 2 stress on
fix             plate_wall all wall/gran model hertz tangential history mesh n_meshes 1 meshes cad

create_atoms    1 single 0.0 0.0 0.05 units box
set             type 1 diameter 0.001 density 1000.0

fix             integr all nve/sphere

timestep        1e-6
run             0
"""


@mcp.tool()
def validate_liggghts_bin(
    run_parse_probe: bool = False,
    run_runtime_probe: bool = False,
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    """Probe the configured LIGGGHTS binary and report whether it can run.

    Returns a dict with:
      liggghts_bin:              path the server will invoke
      exists:                    file is present
      executable:                file is executable by current user
      version_line:              first non-empty line of `liggghts -help` (or stderr)
      mesh_surface:              True if `mesh/surface` (any flavor) is present
      mesh_surface_stress:       True if `mesh/surface/stress` specifically is
                                 present — the plate workflow REQUIRES this.
                                 Some apt builds expose `mesh/surface` but NOT
                                 `mesh/surface/stress`, in which case the plate
                                 pipeline will fail at deck parse time.
      mesh_surface_stress_probe: alias of mesh_surface_stress (back-compat for
                                 v0.2.1 callers; will be removed in a future
                                 release).
      error:                     short error string when something fails
      mpi_used:                  True when the probes were launched through
                                 `mpirun -np mpi_ranks`
      mpi_ranks_per_case:        rank count used for the probe commands
      mpi_launcher:              launcher prefix, e.g. `mpirun -np 4`

    When `run_parse_probe=True`, additionally execute a minimal self-contained
    deck (a 2-triangle STL + a single `fix mesh/surface/stress ... stress on`
    line) inside a tempdir to verify the binary actually accepts that fix at
    parse time. The deck is intentionally bare — no atoms, no pair_style, no
    `run` — so unrelated parser errors (atom-sort bin sizing, missing material
    properties, etc.) cannot pollute the signal. Exit 0 = fix style present
    and constructor succeeded (STL was read); non-zero with "Invalid fix
    style" in the tail = feature missing. The grep-based `mesh_surface_stress`
    flag can be wrong in BOTH directions on apt builds — `-help` may omit a
    style that actually works, or list one the parser rejects — so the parse
    probe is the authoritative signal. The probe never depends on any project
    file or PINN directory.

    When `run_parse_probe=True` adds these extra fields:
      mesh_surface_stress_parse_probe: True iff the probe deck exited 0
      parse_probe_exit_code:           probe `liggghts -in` exit code (or -1
                                       if it timed out or failed to spawn)
      parse_probe_work_dir:            tempdir path (kept on disk for debug)
      parse_probe_error_tail:          last ~2KB of probe stdout+stderr;
                                       empty string when probe succeeded

    `run_runtime_probe=True` is an INDEPENDENT capability check on top of the
    parse probe. It executes a separate self-contained deck that adds:
      - 2-type material property matrix (`property/global` Young / Poisson /
        restitution / friction)
      - `pair_style gran model hertz tangential history` + `pair_coeff * *`
      - `fix wall/gran model hertz tangential history mesh n_meshes 1 meshes cad`
      - one seed atom + `nve/sphere` integrator (LIGGGHTS refuses `run 0` with
        zero atoms)
      - `neigh_modify delay 0` (granular pair-style requirement)
      - `timestep 1e-6` + `run 0`
    so a `run 0` actually constructs the contact model and the mesh wall. This
    catches builds where `mesh/surface/stress` parses fine but `wall/gran ...
    mesh` or one of the granular material-property keywords is wired up
    differently — failure modes the parse probe by design cannot see. The two
    probes are orthogonal: each is bare-minimum for its own signal, so a
    failure pinpoints which layer is broken instead of conflating parser-level
    and runtime-level breakage. The deck is still self-contained — no project
    files, no PINN dir.

    When `run_runtime_probe=True` adds these extra fields:
      runtime_probe_ok:         True iff the runtime probe deck exited 0
      runtime_probe_exit_code:  probe `liggghts -in` exit code (or -1 on
                                timeout / spawn failure)
      runtime_probe_work_dir:   tempdir path (kept on disk for debug)
      runtime_probe_error_tail: last ~2KB of probe stdout+stderr; empty
                                string when probe succeeded
    """
    _validate_rank_count(mpi_ranks, "mpi_ranks")
    info: dict = {
        "liggghts_bin": LIGGGHTS_BIN,
        "exists": False,
        "executable": False,
        "version_line": None,
        "mesh_surface": None,
        "mesh_surface_stress": None,
        "mesh_surface_stress_probe": None,
        **_mpi_status_fields(mpi_ranks, mpirun),
    }
    p = Path(LIGGGHTS_BIN)
    info["exists"] = p.is_file()
    if not info["exists"]:
        info["error"] = f"binary not found at {LIGGGHTS_BIN}"
        return info
    info["executable"] = os.access(p, os.X_OK)
    if not info["executable"]:
        info["error"] = f"binary not executable: {LIGGGHTS_BIN}"
        return info
    try:
        proc = subprocess.run(
            _liggghts_cmd(["-help"], mpi_ranks, mpirun),
            capture_output=True,
            text=True,
            timeout=10,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        info["error"] = "liggghts -help timed out after 10s"
        return info
    except OSError as e:
        info["error"] = f"failed to spawn liggghts command: {e}"
        return info
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in blob.splitlines():
        s = line.strip()
        if s:
            info["version_line"] = s
            break
    low = blob.lower()
    info["mesh_surface_stress"] = "mesh/surface/stress" in low
    info["mesh_surface"] = info["mesh_surface_stress"] or "mesh/surface" in low
    info["mesh_surface_stress_probe"] = info["mesh_surface_stress"]

    if run_parse_probe:
        tmpdir = Path(tempfile.mkdtemp(prefix="liggghts_parse_probe_"))
        info["parse_probe_work_dir"] = str(tmpdir)
        (tmpdir / "plate_probe.stl").write_text(_PARSE_PROBE_STL)
        (tmpdir / "probe.in").write_text(_PARSE_PROBE_DECK)
        try:
            pp = subprocess.run(
                _liggghts_cmd(["-in", "probe.in"], mpi_ranks, mpirun),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                cwd=str(tmpdir),
                env=_child_env(),
                stdin=subprocess.DEVNULL,
            )
            info["parse_probe_exit_code"] = pp.returncode
            tail_blob = (pp.stdout or "") + (pp.stderr or "")
            info["mesh_surface_stress_parse_probe"] = pp.returncode == 0
            info["parse_probe_error_tail"] = (
                "" if pp.returncode == 0 else tail_blob[-2048:]
            )
        except subprocess.TimeoutExpired as e:
            info["mesh_surface_stress_parse_probe"] = False
            info["parse_probe_exit_code"] = -1
            tail_blob = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""
            info["parse_probe_error_tail"] = (
                "TIMEOUT after 30s\n" + (tail_blob[-2048:] if tail_blob else "")
            )
        except OSError as e:
            info["mesh_surface_stress_parse_probe"] = False
            info["parse_probe_exit_code"] = -1
            info["parse_probe_error_tail"] = f"failed to spawn liggghts command: {e}"

    if run_runtime_probe:
        tmpdir = Path(tempfile.mkdtemp(prefix="liggghts_runtime_probe_"))
        info["runtime_probe_work_dir"] = str(tmpdir)
        (tmpdir / "plate_probe.stl").write_text(_PARSE_PROBE_STL)
        (tmpdir / "probe.in").write_text(_RUNTIME_PROBE_DECK)
        try:
            rp = subprocess.run(
                _liggghts_cmd(["-in", "probe.in"], mpi_ranks, mpirun),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
                cwd=str(tmpdir),
                env=_child_env(),
                stdin=subprocess.DEVNULL,
            )
            info["runtime_probe_exit_code"] = rp.returncode
            tail_blob = (rp.stdout or "") + (rp.stderr or "")
            info["runtime_probe_ok"] = rp.returncode == 0
            info["runtime_probe_error_tail"] = (
                "" if rp.returncode == 0 else tail_blob[-2048:]
            )
        except subprocess.TimeoutExpired as e:
            info["runtime_probe_ok"] = False
            info["runtime_probe_exit_code"] = -1
            tail_blob = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""
            info["runtime_probe_error_tail"] = (
                "TIMEOUT after 60s\n" + (tail_blob[-2048:] if tail_blob else "")
            )
        except OSError as e:
            info["runtime_probe_ok"] = False
            info["runtime_probe_exit_code"] = -1
            info["runtime_probe_error_tail"] = f"failed to spawn liggghts command: {e}"
    return info


@mcp.tool()
def start_simulation(
    input_script: str,
    num_procs: int = 1,
    name: str | None = None,
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Start LIGGGHTS in the background and return a run_id immediately.

    input_script: full text of the LIGGGHTS input deck (written to input.in).
    num_procs:    >1 launches via mpirun -np N.
    name:         optional run_id; auto-generated if omitted.
    overwrite:    if False (default), refuse to start when a run dir for
                  `name` already exists. If True, clear the existing dir
                  (only when the prior run is not currently running) before
                  starting. A still-running prior run is always rejected
                  regardless of this flag.
    mpirun:       MPI launcher executable/options. The server appends
                  `-np num_procs` when num_procs > 1.
    """
    _validate_rank_count(num_procs, "num_procs")
    run_id = name or uuid.uuid4().hex[:8]
    wd = _run_dir(run_id)
    if wd.exists():
        pid = _read_pid(wd)
        if pid and _alive(pid):
            raise RuntimeError(f"run_id {run_id} is already running")
        if not overwrite:
            raise RuntimeError(
                f"run_id {run_id} already exists at {wd}; "
                f"pass overwrite=True to clear it or choose a different name"
            )
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    (wd / "input.in").write_text(input_script)

    cmd = _liggghts_cmd(["-in", "input.in"], num_procs, mpirun)

    return _launch(
        wd,
        cmd,
        run_id=run_id,
        extra_status={
            "num_procs": num_procs,
            "mpi_ranks": num_procs,
            **_mpi_status_fields(num_procs, mpirun),
        },
    )


@mcp.tool()
def start_from_file(
    input_path: str,
    num_procs: int = 1,
    name: str | None = None,
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Start a run from an existing input deck file. Copies it into the run dir."""
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(input_path)
    return start_simulation(
        src.read_text(), num_procs=num_procs, name=name, overwrite=overwrite, mpirun=mpirun
    )


@mcp.tool()
def start_from_dir(
    case_dir: str,
    deck_relpath: str,
    name: str | None = None,
    num_procs: int = 1,
    link_mode: str = "copy",
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Snapshot a whole case directory into the run dir, then run LIGGGHTS on a deck inside it.

    case_dir:     directory containing all case assets (STL, .liggghts deck,
                  .lammpstrj initial state, case.json, etc.).
    deck_relpath: path to the LIGGGHTS input deck *relative to case_dir*.
    name:         optional run_id; auto-generated if omitted.
    num_procs:    >1 launches via mpirun -np N.
    mpirun:       MPI launcher executable/options. The server appends
                  `-np num_procs` when num_procs > 1.
    link_mode:    "copy" (default, RECOMMENDED) makes a full isolated copy —
                  the run dir is independent of the source. Outputs land in
                  the run dir, source case_dir is untouched.

                  "symlink" creates per-file symlinks INSTEAD of copies. This
                  is dangerous when LIGGGHTS opens any of those files for
                  writing or appending: the write follows the symlink and
                  CORRUPTS the original file in case_dir. LIGGGHTS does this
                  for things like restart files, fix print outputs, and any
                  dump path that happens to collide with an existing input
                  filename. Outputs that LIGGGHTS creates fresh (new
                  filenames) are written into the run dir as expected.

                  Use "symlink" ONLY when:
                    - inputs are large and you want to avoid the copy cost,
                    - AND you have audited the deck and confirmed it never
                      writes to or modifies any input file path,
                    - AND you accept the risk that a deck change later could
                      silently overwrite the source case.
                  When in doubt, stick with "copy".
    overwrite:    if False (default), refuse to start when a run dir for
                  `name` already exists. If True, clear the existing dir
                  (only when the prior run is not currently running) before
                  re-snapshotting. A still-running prior run is always
                  rejected regardless of this flag.
    """
    src = Path(case_dir).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"case_dir not a directory: {case_dir}")
    deck_rel = Path(deck_relpath)
    if deck_rel.is_absolute() or ".." in deck_rel.parts:
        raise ValueError("deck_relpath must be relative and stay inside case_dir")
    if not (src / deck_rel).is_file():
        raise FileNotFoundError(f"deck not found: {src / deck_rel}")
    if link_mode not in ("copy", "symlink"):
        raise ValueError("link_mode must be 'copy' or 'symlink'")
    _validate_rank_count(num_procs, "num_procs")

    run_id = name or uuid.uuid4().hex[:8]
    wd = _run_dir(run_id)
    if wd.exists():
        pid = _read_pid(wd)
        if pid and _alive(pid):
            raise RuntimeError(f"run_id {run_id} is already running")
        if not overwrite:
            raise RuntimeError(
                f"run_id {run_id} already exists at {wd}; "
                f"pass overwrite=True to clear it or choose a different name"
            )
        shutil.rmtree(wd)

    if link_mode == "copy":
        shutil.copytree(src, wd)
    else:
        wd.mkdir(parents=True)
        for root, dirs, files in os.walk(src):
            rel_root = Path(root).relative_to(src)
            for d in dirs:
                (wd / rel_root / d).mkdir(parents=True, exist_ok=True)
            for fname in files:
                target = Path(root) / fname
                link = wd / rel_root / fname
                link.symlink_to(target.resolve())

    cmd = _liggghts_cmd(["-in", str(deck_rel)], num_procs, mpirun)

    return _launch(
        wd,
        cmd,
        run_id=run_id,
        extra_status={
            "kind": "start_from_dir",
            "case_dir": str(src),
            "deck_relpath": str(deck_rel),
            "link_mode": link_mode,
            "num_procs": num_procs,
            "mpi_ranks": num_procs,
            **_mpi_status_fields(num_procs, mpirun),
        },
    )


@mcp.tool()
def run_plate_case(
    case_dir: str,
    pinn_root: str = "/home/boran/PINN",
    name: str | None = None,
    postprocess: bool = False,
    overwrite: bool = False,
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    """Run the full plate-reference pipeline in-place on a case dir.

    Invokes hpc/liggghts_plate_tier1/run_one_case.sh (settlement → plate_z0
    recompute → intrusion). Writes outputs *into the case dir itself* (the
    bash script does `cd "$case_dir"`), and captures stdout+stderr into the
    MCP run dir's log.run.

    If postprocess=True, also runs scripts/postprocess_liggghts_plate_hpc_sweep.py
    --case-id <basename(case_dir)> --output <per-case csv>. The per-case CSV
    lives at `{pinn}/outputs/plate_reference/{case_id}_summary.csv` so each
    run writes its own file instead of clobbering the sweep-wide default.
    The path is recorded as `status.summary_csv`.

    NOTE: postprocess=True requires `basename(case_dir)` to be registered in
    the PINN sweep manifest (default
    `outputs/plate_reference/liggghts_plate_hpc_tier1_manifest.csv`). The
    postprocessor filters manifest rows by `--case-id` and exits 2 if no row
    matches — a 30+ minute simulation will succeed and then the postprocess
    phase will fail. Smoke runs on arbitrary case dirs should leave the
    default postprocess=False.

    case_dir:    absolute path to the case directory.
    pinn_root:   PINN repo root (default /home/boran/PINN).
    name:        optional run_id.
    postprocess: also run the sweep postprocessor (default False — only safe
                 when basename(case_dir) is in the sweep manifest).
    mpi_ranks:   MPI ranks per case. When >1, sets LIGGGHTS_LAUNCHER to
                 `{mpirun} -np {mpi_ranks}` for the project runner.
    mpirun:      MPI launcher executable/options. The server appends
                 `-np mpi_ranks` when mpi_ranks > 1.
    overwrite:   if False (default), refuse to start when a run dir for
                 `name` already exists. If True, clear the existing dir
                 (only when the prior run is not currently running) before
                 starting. A still-running prior run is always rejected
                 regardless of this flag. Note: this only affects the MCP
                 run dir; outputs the runner writes into `case_dir` itself
                 are not touched.
    """
    _validate_rank_count(mpi_ranks, "mpi_ranks")
    case = Path(case_dir).expanduser().resolve()
    if not case.is_dir():
        raise FileNotFoundError(f"case_dir not a directory: {case_dir}")
    pinn = Path(pinn_root).expanduser().resolve()
    if not pinn.is_dir():
        raise FileNotFoundError(f"pinn_root not a directory: {pinn_root}")
    runner = pinn / "hpc" / "liggghts_plate_tier1" / "run_one_case.sh"
    if not runner.is_file():
        raise FileNotFoundError(f"runner not found: {runner}")

    case_id = case.name
    run_id = name or f"plate_{case_id}_{uuid.uuid4().hex[:6]}"
    wd = _run_dir(run_id)
    if wd.exists():
        pid = _read_pid(wd)
        if pid and _alive(pid):
            raise RuntimeError(f"run_id {run_id} is already running")
        if not overwrite:
            raise RuntimeError(
                f"run_id {run_id} already exists at {wd}; "
                f"pass overwrite=True to clear it or choose a different name"
            )
        shutil.rmtree(wd)
    wd.mkdir(parents=True)

    # Wrapper script: run the pipeline, then optionally postprocess. Both
    # phases share the same log.run via _launch's stdout redirect. All
    # interpolated paths are shlex-quoted so spaces / special chars in
    # case_dir or pinn_root don't corrupt the command.
    postproc = pinn / "scripts" / "postprocess_liggghts_plate_hpc_sweep.py"
    summary_csv = pinn / "outputs" / "plate_reference" / f"{case_id}_summary.csv"
    q_runner = shlex.quote(str(runner))
    q_case = shlex.quote(str(case))
    q_case_id = shlex.quote(case_id)
    parts = [
        "set -e",
        f'echo "[mcp] phase=run_one_case case_id={q_case_id}"',
        f"bash {q_runner} {q_case}",
    ]
    if postprocess:
        if not postproc.is_file():
            raise FileNotFoundError(f"postprocess script not found: {postproc}")
        q_postproc = shlex.quote(str(postproc))
        q_summary_csv = shlex.quote(str(summary_csv))
        parts += [
            f'echo "[mcp] phase=postprocess case_id={q_case_id}"',
            f"mkdir -p {shlex.quote(str(summary_csv.parent))}",
            f"python3 {q_postproc} --case-id {q_case_id} --output {q_summary_csv}",
        ]
    parts.append(f'echo "[mcp] phase=done case_id={q_case_id}"')
    script = "\n".join(parts)
    (wd / "wrapper.sh").write_text(script)

    extra_status = {
        "kind": "run_plate_case",
        "case_dir": str(case),
        "case_id": case_id,
        "case_dir_outputs": str(case),
        "pinn_root": str(pinn),
        "postprocess": postprocess,
        **_mpi_status_fields(mpi_ranks, mpirun),
    }
    if postprocess:
        extra_status["summary_csv"] = str(summary_csv)

    cmd = ["bash", str(wd / "wrapper.sh")]
    return _launch(
        wd,
        cmd,
        run_id=run_id,
        cwd=pinn,
        env_extra=_plate_env(pinn, mpi_ranks, mpirun),
        extra_status=extra_status,
    )


@mcp.tool()
def check_status(run_id: str) -> dict:
    """Return running/finished/unknown for a run, plus last log line and exit code.

    Reads status.json so completed runs survive MCP server restarts.
    """
    wd = _run_dir(run_id)
    status = _read_status(wd)
    if not status:
        # Legacy run with no status.json — fall back to pid+log only.
        pid = _read_pid(wd)
        if pid is None:
            return {"run_id": run_id, "status": "unknown"}
        live = _alive(pid)
        return {
            "run_id": run_id,
            "status": "running" if live else "finished",
            "pid": pid,
            "last_log": _last_log_line(wd),
        }
    status = _finalize_if_done(run_id, wd, status)
    status = {**status, "last_log": _last_log_line(wd)}
    return status


@mcp.tool()
def read_log(run_id: str, tail: int = 200) -> str:
    """Return the last `tail` lines of log.run for a run."""
    wd = _run_dir(run_id)
    log = wd / "log.run"
    if not log.exists():
        return f"no log for {run_id}"
    lines = log.read_text(errors="replace").splitlines()
    return "\n".join(lines[-tail:])


_DEFAULT_OUTPUT_PATTERNS = (
    "*.dump",
    "*.lammpstrj",
    "*.csv",
    "*.json",
    "*.log",
    "log.*",
    "screen.*",
    "*.vtk",
    "*.vtp",
    "*.restart",
    "post/*",
)


@mcp.tool()
def list_outputs(
    run_id: str,
    patterns: list[str] | None = None,
    include_bookkeeping: bool = False,
) -> list[str]:
    """List output files in a run dir matching the given glob patterns.

    Default patterns cover dumps, trajectories, CSV/JSON summaries, and logs:
    *.dump, *.lammpstrj, *.csv, *.json, *.log, log.*, screen.*, *.vtk, *.vtp,
    *.restart, post/*. Pass `patterns` to override.

    By default excludes the MCP server's own bookkeeping files (pid, cmd,
    status.json, log.run, wrapper.sh, input.in, exit_code). Pass
    `include_bookkeeping=True` to surface them — useful for the agent to
    self-debug by reading status.json / exit_code / log.run directly.

    For runs of kind "run_plate_case", also globs `status.case_dir_outputs`
    (the external case dir where `run_one_case.sh` actually writes outputs)
    and merges those paths into the same sorted list. The MCP run dir for
    plate runs only holds the wrapper + log; the real .csv / .lammpstrj /
    .json land in the case dir.
    """
    wd = _run_dir(run_id)
    pats = patterns or list(_DEFAULT_OUTPUT_PATTERNS)
    bookkeeping = {
        "pid", "cmd", "status.json", "log.run", "wrapper.sh", "input.in",
        "exit_code",
    }
    seen: set[str] = set()

    search_dirs = [wd]
    status = _read_status(wd)
    extra = status.get("case_dir_outputs")
    if extra:
        try:
            extra_path = Path(extra).expanduser().resolve()
        except (OSError, ValueError):
            extra_path = None
        if extra_path and extra_path.is_dir() and extra_path != wd.resolve():
            search_dirs.append(extra_path)

    for d in search_dirs:
        for pat in pats:
            for p in d.glob(pat):
                if not include_bookkeeping and p.name in bookkeeping:
                    continue
                seen.add(str(p))
    return sorted(seen)


@mcp.tool()
def list_dumps(run_id: str) -> list[str]:
    """List dump/restart/post files (alias for list_outputs with dump-only patterns)."""
    return list_outputs(run_id, patterns=["*.dump", "*.vtk", "*.vtp", "*.restart", "post/*"])


@mcp.tool()
def list_runs() -> list[dict]:
    """List all known runs and their current status (uses status.json when present)."""
    out = []
    for wd in sorted(RUNS.iterdir()):
        if not wd.is_dir():
            continue
        status = _read_status(wd)
        if status:
            status = _finalize_if_done(wd.name, wd, status)
            out.append({
                "run_id": wd.name,
                "status": status.get("status", "unknown"),
                "pid": status.get("pid"),
                "exit_code": status.get("exit_code"),
                "kind": status.get("kind", "start_simulation"),
            })
            continue
        pid = _read_pid(wd)
        s = "unknown"
        if pid is not None:
            s = "running" if _alive(pid) else "finished"
        out.append({"run_id": wd.name, "status": s, "pid": pid})
    return out


@mcp.tool()
def stop_simulation(run_id: str) -> dict:
    """Send SIGTERM to a running simulation (SIGKILL if it ignores).

    Records `terminated_by_mcp: true` and `termination_signal:
    "SIGTERM"|"SIGKILL"` in status.json so postprocess steps can distinguish
    a user-requested stop from a simulation crash. SIGTERM is recorded if the
    process exited within the 2s grace period; SIGKILL otherwise.
    """
    wd = _run_dir(run_id)
    pid = _read_pid(wd)
    if pid is None or not _alive(pid):
        return {"run_id": run_id, "status": "not_running"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return {"run_id": run_id, "status": "not_running"}
    # Best-effort wait; don't block MCP for long.
    for _ in range(20):
        time.sleep(0.1)
        if not _alive(pid):
            break
    sig_used = "SIGTERM"
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            sig_used = "SIGKILL"
        except ProcessLookupError:
            pass
    status = _read_status(wd)
    if status:
        status = _finalize_if_done(run_id, wd, status)
        status["terminated_by_mcp"] = True
        status["termination_signal"] = sig_used
        if status.get("status") == "running":
            status["status"] = "finished"
            status["finished_at"] = _now_iso()
            status["exit_code"] = status.get("exit_code", -signal.SIGTERM)
        _write_status(wd, status)
    return {
        "run_id": run_id,
        "status": "terminated",
        "pid": pid,
        "termination_signal": sig_used,
    }


@mcp.tool()
def validate_project_environment(
    pinn_root: str,
    require_serial_liggghts: bool = True,
    run_parse_probe: bool = True,
    run_runtime_probe: bool = True,
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    """Validate PINN project paths, LIGGGHTS binary, scripts, and safety metadata."""
    _validate_rank_count(mpi_ranks, "mpi_ranks")
    pinn = _resolve_pinn(pinn_root)
    lig = validate_liggghts_bin(
        run_parse_probe=run_parse_probe,
        run_runtime_probe=run_runtime_probe,
        mpi_ranks=mpi_ranks,
        mpirun=mpirun,
    )
    missing = [rel for rel in _PROJECT_REQUIRED_PATHS if not (pinn / rel).exists()]
    env_path = pinn / "hpc" / "liggghts_plate_tier1" / "environment.sh"
    write_probe = RUNS / f".write_probe_{uuid.uuid4().hex[:8]}"
    run_root_writable = False
    try:
        write_probe.write_text("ok")
        write_probe.unlink()
        run_root_writable = True
    except OSError:
        run_root_writable = False
    gpu_visible = bool(os.environ.get("CUDA_VISIBLE_DEVICES"))
    version_line = str(lig.get("version_line") or "")
    vlow = version_line.lower()
    bin_low = LIGGGHTS_BIN.lower()
    mpi_markers = ("mpi build", "mpi version", "with mpi", "openmpi", "mpich", "intel mpi")
    serial_liggghts = (
        not any(m in vlow for m in mpi_markers)
        and "mpirun" not in bin_low
        and "/mpi/" not in bin_low
    )
    probe_ok = True
    if run_parse_probe:
        probe_ok = probe_ok and bool(lig.get("mesh_surface_stress_parse_probe"))
    if run_runtime_probe:
        probe_ok = probe_ok and bool(lig.get("runtime_probe_ok"))
    ready = (
        not missing
        and bool(lig.get("exists"))
        and bool(lig.get("executable"))
        and probe_ok
        and run_root_writable
        and (serial_liggghts or not require_serial_liggghts)
    )
    return {
        "pinn_root": str(pinn),
        "python_version": sys.version.split()[0],
        "liggghts_bin": LIGGGHTS_BIN,
        "liggghts_validation": lig,
        "serial_liggghts": serial_liggghts,
        "require_serial_liggghts": require_serial_liggghts,
        "required_scripts": list(_PROJECT_REQUIRED_PATHS),
        "missing_scripts": missing,
        "environment_sh": str(env_path),
        "environment_sh_exists": env_path.exists(),
        "run_root": str(RUNS),
        "run_root_writable": run_root_writable,
        "gpu_visible": gpu_visible,
        "gpu_used": False,
        **_mpi_status_fields(mpi_ranks, mpirun),
        "ready": ready,
    }


@mcp.tool()
def run_tilt_geometry_gates(
    pinn_root: str,
    tilts_deg: list[float],
    workers: int = 1,
    overwrite: bool = False,
) -> dict:
    """Run available single-face geometry gates and block broad tilt batches when true per-tilt gates are unavailable."""
    pinn = _resolve_pinn(pinn_root)
    gate_id = "geometry_gates_" + uuid.uuid5(uuid.NAMESPACE_URL, str(pinn)).hex[:12]
    wd = _batch_dir(gate_id)
    if wd.exists():
        pid = _read_pid(wd)
        if pid and _alive(pid):
            raise RuntimeError(f"geometry gate {gate_id} is already running")
        if not overwrite:
            existing = _read_json(wd / "geometry_gate_status.json")
            if existing:
                return existing
            raise RuntimeError(f"geometry gate dir exists at {wd}; pass overwrite=True")
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    tilted_gen = pinn / "scripts" / "generate_liggghts_plate_tilted_geometry_unit_tests.py"
    tilted_post = pinn / "scripts" / "postprocess_liggghts_plate_tilted_geometry_unit_tests.py"
    gen = pinn / "scripts" / "generate_liggghts_plate_geometry_unit_tests.py"
    post = pinn / "scripts" / "postprocess_liggghts_plate_geometry_unit_tests.py"
    gate_rows = []
    commands = []
    rc = 0
    summary_rows = []
    if tilted_gen.is_file() and tilted_post.is_file():
        code = f"""
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("tilted_gate", {str(tilted_gen)!r})
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
mod.TILT_DEGREES = tuple({[float(t) for t in tilts_deg]!r})
liggghts = Path({LIGGGHTS_BIN!r})
mod.generate(liggghts)
mod.run_tests(liggghts)
"""
        for cmd in (
            [sys.executable, "-c", code],
            [sys.executable, str(tilted_post)],
        ):
            proc = subprocess.run(
                cmd,
                cwd=str(pinn),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=_child_env({"LIGGGHTS_BIN": LIGGGHTS_BIN, "PINN_ROOT": str(pinn)}),
                stdin=subprocess.DEVNULL,
            )
            cmd_text = shlex.join(cmd[:2]) + (" <tilted-gate-snippet>" if cmd[1] == "-c" else "")
            commands.append({"cmd": cmd_text, "exit_code": proc.returncode, "output_tail": proc.stdout[-2048:]})
            if proc.returncode != 0:
                rc = proc.returncode
                break
        summary_csv = pinn / "outputs" / "plate_reference" / "liggghts_plate_tilted_geometry_unit_tests_summary.csv"
        summary_rows = _read_manifest(summary_csv) if summary_csv.exists() else []
        for tilt in tilts_deg:
            rows_for_tilt = [
                r for r in summary_rows
                if abs(float(r.get("tilt_deg") or "nan") - float(tilt)) < 1.0e-9
            ]
            passed = rc == 0 and bool(rows_for_tilt) and all(
                str(r.get("pass_geometry_check", "")).lower() == "true"
                for r in rows_for_tilt
            )
            gate_rows.append({
                "tilt_deg": tilt,
                "status": "passed" if passed else "failed",
                "passed": passed,
                "supported_by_local_gate": bool(rows_for_tilt),
                "tested_contact_count": len(rows_for_tilt),
                "reason": "tilted single-face geometry gate passed" if passed else "tilted geometry gate failed or missing rows",
            })
    elif gen.is_file() and post.is_file():
        for cmd in (
            [sys.executable, str(gen), "--liggghts", LIGGGHTS_BIN, "--run"],
            [sys.executable, str(post)],
        ):
            proc = subprocess.run(
                cmd,
                cwd=str(pinn),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=_child_env({"LIGGGHTS_BIN": LIGGGHTS_BIN, "PINN_ROOT": str(pinn)}),
                stdin=subprocess.DEVNULL,
            )
            commands.append({"cmd": shlex.join(cmd), "exit_code": proc.returncode, "output_tail": proc.stdout[-2048:]})
            if proc.returncode != 0:
                rc = proc.returncode
                break
        base_gate_passed = rc == 0
        for tilt in tilts_deg:
            supported = float(tilt) == 0.0
            passed = base_gate_passed and supported
            gate_rows.append({
                "tilt_deg": tilt,
                "status": "passed" if passed else "failed",
                "passed": passed,
                "supported_by_local_gate": supported,
                "reason": "single-face zero-tilt geometry gate passed" if passed else "true per-tilt gate is not available in this PINN checkout",
            })
    else:
        rc = 2
        commands.append({"error": "geometry unit-test scripts missing"})
        for tilt in tilts_deg:
            gate_rows.append({
                "tilt_deg": tilt,
                "status": "failed",
                "passed": False,
                "supported_by_local_gate": False,
                "reason": "geometry unit-test scripts missing",
            })
    failed = [r["tilt_deg"] for r in gate_rows if not r["passed"]]
    result = {
        "gate_id": gate_id,
        "pinn_root": str(pinn),
        "workers": workers,
        "gate_rows": gate_rows,
        "passed_count": len(gate_rows) - len(failed),
        "failed_count": len(failed),
        "failed_tilts": failed,
        "ready_for_dem": len(failed) == 0,
        "commands": commands,
        "gpu_used": False,
        "is_reference_quality": False,
        "is_training_data": False,
    }
    _write_json(wd / "geometry_gate_status.json", result)
    _write_json(wd / "batch_metadata.json", result)
    return result


def _run_plate_manifest_batch_sync(
    pinn: Path,
    manifest: Path,
    batch_id: str,
    workers: int,
    require_geometry_gate: bool,
    raw_dump_policy: str,
    overwrite: bool,
    resume: bool,
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    _validate_rank_count(mpi_ranks, "mpi_ranks")
    if raw_dump_policy != "do_not_package":
        raise ValueError("only raw_dump_policy='do_not_package' is supported")
    geometry_gate = _geometry_gate_ready(pinn)
    if require_geometry_gate and not geometry_gate["ready_for_dem"]:
        raise RuntimeError(
            "required geometry gate is not ready for DEM; "
            f"gate_id={geometry_gate['gate_id']!r} failed_tilts={geometry_gate['failed_tilts']}"
        )
    rows = _read_manifest(manifest)
    unsafe = [r.get("case_id", "") for r in rows if str(r.get("is_reference_quality", "")).lower() == "true" or str(r.get("is_training_data", "")).lower() == "true"]
    if unsafe:
        raise RuntimeError(f"manifest attempts reference/training promotion: {unsafe}")
    runner = pinn / "hpc" / "liggghts_plate_tier1" / "run_one_case.sh"
    generator = pinn / "scripts" / "generate_liggghts_plate_hpc_cases.py"
    if not runner.is_file():
        raise FileNotFoundError(f"runner not found: {runner}")
    if not generator.is_file():
        raise FileNotFoundError(f"generator not found: {generator}")
    lig = validate_liggghts_bin(
        run_parse_probe=False,
        run_runtime_probe=False,
        mpi_ranks=mpi_ranks,
        mpirun=mpirun,
    )
    if not lig.get("exists") or not lig.get("executable") or lig.get("error"):
        raise RuntimeError(f"LIGGGHTS binary preflight failed: {lig}")
    wd = _batch_dir(batch_id)
    if wd.exists() and overwrite and not resume:
        pid = _read_pid(wd)
        if pid and _alive(pid):
            raise RuntimeError(f"batch_id {batch_id} is already running")
        shutil.rmtree(wd)
    wd.mkdir(parents=True, exist_ok=True)
    metadata = {
        "batch_id": batch_id,
        "kind": "plate_manifest_batch",
        "pinn_root": str(pinn),
        "manifest_path": str(manifest),
        "manifest_rows": len(rows),
        "workers": workers,
        "require_geometry_gate": require_geometry_gate,
        "raw_dump_policy": raw_dump_policy,
        "geometry_gate": geometry_gate,
        "gpu_used": False,
        "raw_dumps_packaged": False,
        "is_reference_quality": False,
        "is_training_data": False,
        "mcp_used": True,
        "mcp_server_version": __version__,
        "liggghts_bin": LIGGGHTS_BIN,
        "started_at": _now_iso(),
        "status": "running",
        **_mpi_status_fields(mpi_ranks, mpirun, workers=workers),
    }
    _write_json(wd / "batch_metadata.json", metadata)
    _write_status(wd, metadata)
    old_rows = {r.get("case_id", ""): r for r in _read_case_status(wd)} if resume else {}
    skipped_completed_cases = 0
    resumed_cases = 0
    case_order = [r.get("case_id", "").strip() for r in rows if r.get("case_id", "").strip()]
    case_status_by_id: dict[str, dict] = {}
    run_tasks: list[dict] = []
    log = wd / "log.run"
    case_log_dir = wd / "case_logs"
    case_log_dir.mkdir(parents=True, exist_ok=True)
    cases_root = pinn / "hpc" / "liggghts_plate_tier1" / "generated_cases"
    batch_log_lock = Lock()

    def ordered_case_status() -> list[dict]:
        return [case_status_by_id[c] for c in case_order if c in case_status_by_id]

    def append_batch_log(line: str) -> None:
        with batch_log_lock:
            with log.open("a", encoding="utf-8") as lf:
                lf.write(line.rstrip() + "\n")

    def persist_progress(status: str = "running") -> None:
        current_rows = ordered_case_status()
        summary = _batch_summary(current_rows)
        progress = {
            **metadata,
            **summary,
            "attempted_cases": len(current_rows),
            "resumed_cases": resumed_cases,
            "skipped_completed_cases": skipped_completed_cases,
            "status": status,
            "case_status_csv": str(wd / "case_status.csv"),
            "status_json": str(wd / "status.json"),
        }
        _write_case_status(wd, current_rows)
        _write_json(wd / "batch_metadata.json", progress)
        _write_status(wd, progress)

    def build_failed_row(
        case_id: str,
        case_dir: Path,
        exit_code: int,
        started: str,
        case_log: Path,
        error: str = "",
    ) -> dict:
        return {
            "case_id": case_id,
            "status": "failed",
            "exit_code": exit_code,
            "settlement_completed": "false",
            "intrusion_completed": "false",
            "liggghts_error": error,
            "dangerous_builds": "",
            "force_csv_exists": "false",
            "contact_count_csv_exists": "false",
            "started_at": started,
            "finished_at": _now_iso(),
            "case_dir": str(case_dir),
            "run_id": f"{batch_id}:{case_id}",
            "case_log": str(case_log),
        }

    append_batch_log(
        f"[mcp-batch] start batch_id={batch_id} workers={workers} "
        f"mpi_ranks_per_case={mpi_ranks} total_ranks={workers * mpi_ranks}"
    )
    for row in rows:
        case_id = row.get("case_id", "").strip()
        if not case_id:
            continue
        previous = old_rows.get(case_id)
        if previous and previous.get("status") == "completed" and str(previous.get("exit_code")) == "0":
            case_status_by_id[case_id] = {**previous, "status": "completed"}
            skipped_completed_cases += 1
            persist_progress()
            continue
        if previous:
            resumed_cases += 1
        case_dir = cases_root / case_id
        case_log = case_log_dir / f"{case_id}.log"
        started = _now_iso()
        append_batch_log(f"[mcp-batch] generate case_id={case_id}")
        with case_log.open("a", encoding="utf-8") as clf:
            clf.write(f"[mcp-case] generate case_id={case_id}\n")
            clf.flush()
            gen_proc = subprocess.run(
                [sys.executable, str(generator), "--generate", "--case-id", case_id],
                cwd=str(pinn), stdout=clf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=_child_env(_plate_env(pinn, mpi_ranks, mpirun)),
            )
        if gen_proc.returncode != 0:
            append_batch_log(
                f"[mcp-batch] generate failed case_id={case_id} exit_code={gen_proc.returncode}"
            )
            case_status_by_id[case_id] = build_failed_row(
                case_id, case_dir, gen_proc.returncode, started, case_log,
                "case generation failed",
            )
            persist_progress()
            continue
        case_status_by_id[case_id] = {
            "case_id": case_id,
            "status": "pending",
            "exit_code": "",
            "started_at": started,
            "finished_at": "",
            "case_dir": str(case_dir),
            "run_id": f"{batch_id}:{case_id}",
            "case_log": str(case_log),
        }
        run_tasks.append({
            "case_id": case_id,
            "case_dir": case_dir,
            "case_log": case_log,
            "started": started,
        })
        persist_progress()

    def run_case(task: dict) -> dict:
        case_id = task["case_id"]
        case_dir = task["case_dir"]
        case_log = task["case_log"]
        started = task["started"]
        append_batch_log(f"[mcp-batch] run_one_case start case_id={case_id}")
        try:
            with case_log.open("a", encoding="utf-8") as clf:
                clf.write(
                    f"[mcp-case] run_one_case case_id={case_id} "
                    f"mpi_launcher={_mpi_launcher_string(mpi_ranks, mpirun)!r}\n"
                )
                clf.flush()
                proc = subprocess.run(
                    ["bash", str(runner), str(case_dir)],
                    cwd=str(pinn), stdout=clf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=_child_env(_plate_env(pinn, mpi_ranks, mpirun)),
                )
            status_row = _case_status_from_dir(case_id, case_dir, f"{batch_id}:{case_id}")
            status_row["exit_code"] = proc.returncode
            status_row["status"] = (
                "completed"
                if proc.returncode == 0 and status_row["status"] == "completed"
                else "failed"
            )
        except OSError as e:
            status_row = build_failed_row(case_id, case_dir, 127, started, case_log, str(e))
        status_row["started_at"] = started
        status_row["finished_at"] = _now_iso()
        status_row["case_log"] = str(case_log)
        append_batch_log(
            f"[mcp-batch] run_one_case done case_id={case_id} "
            f"status={status_row['status']} exit_code={status_row['exit_code']}"
        )
        return status_row

    if run_tasks:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_case, task): task["case_id"] for task in run_tasks}
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    case_status_by_id[case_id] = future.result()
                except Exception as e:
                    task = next(t for t in run_tasks if t["case_id"] == case_id)
                    case_status_by_id[case_id] = build_failed_row(
                        case_id,
                        task["case_dir"],
                        1,
                        task["started"],
                        task["case_log"],
                        f"worker exception: {e}",
                    )
                    append_batch_log(f"[mcp-batch] worker exception case_id={case_id}: {e}")
                persist_progress()

    case_status = ordered_case_status()
    summary = _batch_summary(case_status)
    failed = summary["failed_cases"] > 0
    metadata.update(summary)
    metadata["attempted_cases"] = len(case_status)
    metadata["resumed_cases"] = resumed_cases
    metadata["skipped_completed_cases"] = skipped_completed_cases
    metadata["finished_at"] = _now_iso()
    metadata["status"] = "failed" if failed else "finished"
    metadata["exit_code"] = 1 if failed else 0
    metadata["case_status_csv"] = str(wd / "case_status.csv")
    metadata["status_json"] = str(wd / "status.json")
    metadata.update(_mpi_status_fields(mpi_ranks, mpirun, workers=workers))
    _write_json(wd / "batch_metadata.json", metadata)
    _write_status(wd, metadata)
    (wd / "exit_code").write_text(str(metadata["exit_code"]))
    append_batch_log(
        f"[mcp-batch] finished batch_id={batch_id} status={metadata['status']} "
        f"completed={summary['completed_cases']} failed={summary['failed_cases']}"
    )
    return metadata


@mcp.tool()
def run_plate_manifest_batch(
    pinn_root: str,
    manifest_path: str,
    batch_id: str,
    workers: int,
    require_geometry_gate: bool = True,
    raw_dump_policy: str = "do_not_package",
    overwrite: bool = False,
    resume: bool = False,
    mpi_ranks: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    """Generate and run LIGGGHTS plate cases from a manifest with persistent batch status.

    `workers` controls how many cases run concurrently. `mpi_ranks` controls
    how many MPI ranks each case receives via LIGGGHTS_LAUNCHER. Keep
    workers * mpi_ranks within the host's useful CPU core count unless you
    intentionally want oversubscription.
    """
    pinn = _resolve_pinn(pinn_root)
    manifest = _resolve_manifest(pinn, manifest_path)
    wd = _batch_dir(batch_id)
    if wd.exists() and not overwrite and not resume:
        status = _read_status(wd)
        if status:
            return status
        raise RuntimeError(f"batch_id {batch_id} already exists at {wd}; pass overwrite=True or resume=True")
    return _run_plate_manifest_batch_sync(
        pinn, manifest, batch_id, workers, require_geometry_gate,
        raw_dump_policy, overwrite, resume, mpi_ranks, mpirun,
    )


@mcp.tool()
def check_batch_status(batch_id: str, tail: int = 50) -> dict:
    """Read persistent batch and case status after MCP restarts."""
    wd = _batch_dir(batch_id)
    status = _read_status(wd) or _read_json(wd / "batch_metadata.json")
    rows = _read_case_status(wd)
    out = {"batch_id": batch_id, "status": "unknown", "exit_code": None}
    if status:
        out.update(status)
    out.update(_batch_summary(rows))
    out["case_status_csv"] = str(wd / "case_status.csv") if (wd / "case_status.csv").exists() else ""
    if (wd / "log.run").exists():
        lines = (wd / "log.run").read_text(errors="replace").splitlines()
        out["last_log"] = lines[-1] if lines else ""
        out["log_tail"] = "\n".join(lines[-tail:])
    else:
        out["last_log"] = ""
        out["log_tail"] = ""
    return out


@mcp.tool()
def list_batch_cases(batch_id: str) -> list[dict]:
    """List persistent per-case status rows for a manifest batch."""
    return _read_case_status(_batch_dir(batch_id))


@mcp.tool()
def resume_plate_manifest_batch(
    batch_id: str,
    failed_only: bool = True,
    workers: int | None = None,
    mpi_ranks: int | None = None,
    mpirun: str | None = None,
) -> dict:
    """Resume a manifest batch without rerunning completed cases."""
    wd = _batch_dir(batch_id)
    metadata = _read_json(wd / "batch_metadata.json")
    if not metadata:
        raise FileNotFoundError(f"batch metadata not found for {batch_id}")
    return _run_plate_manifest_batch_sync(
        _resolve_pinn(metadata["pinn_root"]),
        _resolve_manifest(_resolve_pinn(metadata["pinn_root"]), metadata["manifest_path"]),
        batch_id,
        workers or int(metadata.get("workers") or 1),
        bool(metadata.get("require_geometry_gate", True)),
        metadata.get("raw_dump_policy", "do_not_package"),
        overwrite=False,
        resume=True,
        mpi_ranks=mpi_ranks or int(metadata.get("mpi_ranks_per_case") or 1),
        mpirun=mpirun or str(metadata.get("mpirun") or "mpirun"),
    )


@mcp.tool()
def postprocess_plate_batch(
    pinn_root: str,
    batch_id: str,
    bins: int = 64,
    include_tilted_smoke_postprocess: bool = False,
) -> dict:
    """Run project postprocessing over a completed manifest batch."""
    pinn = _resolve_pinn(pinn_root)
    script = pinn / "scripts" / "postprocess_liggghts_plate_hpc_sweep.py"
    if not script.is_file():
        raise FileNotFoundError(f"postprocess script not found: {script}")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(pinn), capture_output=True, text=True, errors="replace",
        stdin=subprocess.DEVNULL,
    )
    summary_csv = pinn / "outputs" / "plate_reference" / "liggghts_plate_hpc_tier1_sweep_summary.csv"
    rows = _read_manifest(summary_csv) if summary_csv.exists() else []
    result = {
        "batch_id": batch_id,
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout[-2048:],
        "stderr_tail": proc.stderr[-2048:],
        "summary_csv": str(summary_csv),
        "completed_cases": sum(1 for r in rows if str(r.get("settlement_completed", "")).lower() == "true" and str(r.get("intrusion_completed", "")).lower() == "true"),
        "completed_stable_cases": sum(1 for r in rows if r.get("status") == "completed_stable"),
        "passed_cases": sum(1 for r in rows if str(r.get("passed", "")).lower() == "true"),
        "failed_cases": sum(1 for r in rows if str(r.get("passed", "")).lower() != "true"),
        "liggghts_error_cases": [r.get("case_id") for r in rows if r.get("liggghts_error")],
        "dangerous_build_cases": [r.get("case_id") for r in rows if int(float(r.get("dangerous_builds") or 0)) > 0],
        "force_csv_files": len(list((pinn / "hpc" / "liggghts_plate_tier1" / "generated_cases").glob("*/plate_force_raw.csv"))),
        "contact_count_csv_files": len(list((pinn / "hpc" / "liggghts_plate_tier1" / "generated_cases").glob("*/contact_local.dump"))),
        "bins": bins,
    }
    if include_tilted_smoke_postprocess:
        tilted = pinn / "scripts" / "postprocess_liggghts_plate_tilted_smoke.py"
        if not tilted.exists():
            result["tilted_smoke_postprocess"] = {"status": "missing"}
        else:
            tilted_proc = subprocess.run(
                [sys.executable, str(tilted)],
                cwd=str(pinn), capture_output=True, text=True, errors="replace",
                stdin=subprocess.DEVNULL,
            )
            result["tilted_smoke_postprocess"] = {
                "status": "finished" if tilted_proc.returncode == 0 else "failed",
                "exit_code": tilted_proc.returncode,
                "stdout_tail": tilted_proc.stdout[-2048:],
                "stderr_tail": tilted_proc.stderr[-2048:],
            }
    return result


@mcp.tool()
def package_compact_return(
    pinn_root: str,
    batch_id: str,
    phase: str,
    include_linux_done: bool = True,
    forbid_raw_dumps: bool = True,
) -> dict:
    """Create one compact tarball and sha256 file for Windows without raw DEM dumps."""
    pinn = _resolve_pinn(pinn_root)
    transfer = pinn / "outputs" / "transfer"
    transfer.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tar_path = transfer / f"{phase}_windows_return_{stamp}.tar.gz"
    sha_path = Path(str(tar_path) + ".sha256")
    batch = _batch_dir(batch_id)
    candidates = []
    for rel in ("outputs/plate_reference", "outputs/mcp", "outputs/pinn_analysis"):
        path = pinn / rel
        if path.exists():
            candidates.append(path)
    if include_linux_done and (pinn / "codex_tasks" / "linux_done.md").exists():
        candidates.append(pinn / "codex_tasks" / "linux_done.md")
    if batch.exists():
        candidates.extend([p for p in (batch / name for name in ("status.json", "batch_metadata.json", "case_status.csv", "geometry_gate_status.json", "exit_code")) if p.exists()])
    metadata = {
        "batch_id": batch_id,
        "phase": phase,
        "gpu_used": False,
        "raw_dumps_packaged": False,
        "is_reference_quality": False,
        "is_training_data": False,
        "mcp_used": True,
        "mcp_server_version": __version__,
        "liggghts_bin": LIGGGHTS_BIN,
    }
    meta_path = transfer / f"{phase}_mcp_return_metadata_{stamp}.json"
    _write_json(meta_path, metadata)
    candidates.append(meta_path)
    with tarfile.open(tar_path, "w:gz") as tar:
        for path in candidates:
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        if forbid_raw_dumps and child.name in _FORBIDDEN_RAW_DUMPS:
                            continue
                        tar.add(child, arcname=str(child.relative_to(pinn)) if pinn in child.parents else child.name)
            elif path.is_file():
                if forbid_raw_dumps and path.name in _FORBIDDEN_RAW_DUMPS:
                    continue
                tar.add(path, arcname=str(path.relative_to(pinn)) if pinn in path.parents else path.name)
    with tarfile.open(tar_path, "r:gz") as tar:
        names = tar.getnames()
    forbidden = [name for name in names if Path(name).name in _FORBIDDEN_RAW_DUMPS]
    package_verified = not forbidden
    if forbidden and forbid_raw_dumps:
        tar_path.unlink(missing_ok=True)
    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest() if tar_path.exists() else ""
    if digest:
        sha_path.write_text(f"{digest}  {tar_path.name}\n")
    return {
        "tar_gz": str(tar_path),
        "sha256": str(sha_path) if sha_path.exists() else "",
        "raw_dumps_packaged": bool(forbidden),
        "forbidden_entries": forbidden,
        "package_verified": package_verified,
    }


if __name__ == "__main__":
    mcp.run()
