"""LIGGGHTS MCP server — start runs, poll status, read logs, list outputs."""
from mcp.server.fastmcp import FastMCP
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

mcp = FastMCP("liggghts")

__version__ = "0.4.0"

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


def _mpi_status_fields(mpi_ranks: int = 1, mpirun: str = "mpirun") -> dict:
    launcher = _mpi_launcher_string(mpi_ranks, mpirun)
    return {
        "mpi_used": mpi_ranks > 1,
        "mpi_ranks_per_case": mpi_ranks,
        "mpi_launcher": launcher,
        "mpirun": mpirun,
    }


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    wd = (RUNS / run_id).resolve()
    if RUNS.resolve() not in wd.parents and wd != RUNS.resolve():
        raise ValueError("run_id escapes runs root")
    return wd


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
solid mesh_probe
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
endsolid mesh_probe
"""


_PARSE_PROBE_DECK = """\
atom_style      granular
units           si
boundary        f f f
region          domain block -0.1 0.1 -0.1 0.1 -0.05 0.15 units box
create_box      1 domain
fix             cad all mesh/surface/stress file mesh_probe.stl type 1 stress on
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

fix             cad all mesh/surface/stress file mesh_probe.stl type 2 stress on
fix             mesh_wall all wall/gran model hertz tangential history mesh n_meshes 1 meshes cad

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
                                 present.
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
    file.

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
    files.

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
        (tmpdir / "mesh_probe.stl").write_text(_PARSE_PROBE_STL)
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
        (tmpdir / "mesh_probe.stl").write_text(_PARSE_PROBE_STL)
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
    """
    wd = _run_dir(run_id)
    pats = patterns or list(_DEFAULT_OUTPUT_PATTERNS)
    bookkeeping = {
        "pid", "cmd", "status.json", "log.run", "wrapper.sh", "input.in",
        "exit_code",
    }
    seen: set[str] = set()

    for pat in pats:
        for p in wd.glob(pat):
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
    "SIGTERM"|"SIGKILL"` in status.json so callers can distinguish
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


if __name__ == "__main__":
    mcp.run()
