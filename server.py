"""LIGGGHTS MCP server — start runs, poll status, read logs, list outputs."""
from mcp.server.fastmcp import FastMCP
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

mcp = FastMCP("liggghts")

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

    Returns updated status dict (mutated in place).
    """
    if status.get("status") != "running":
        return status
    pid = status.get("pid")
    proc = _PROCS.get(run_id)
    rc: int | None = None
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return status
    else:
        if pid is not None and _alive(pid):
            return status
    status["status"] = "finished"
    status["exit_code"] = rc
    status["finished_at"] = _now_iso()
    _write_status(wd, status)
    return status


def _launch(
    wd: Path,
    cmd: list[str],
    *,
    run_id: str,
    extra_status: dict | None = None,
    env_extra: dict | None = None,
    cwd: Path | None = None,
) -> dict:
    """Spawn a background process, write pid/cmd/status.json, register in _PROCS."""
    log = open(wd / "log.run", "w")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd or wd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=_child_env(env_extra),
    )
    (wd / "pid").write_text(str(proc.pid))
    cmd_str = " ".join(cmd)
    (wd / "cmd").write_text(cmd_str)
    _PROCS[run_id] = proc
    status = {
        "run_id": run_id,
        "pid": proc.pid,
        "status": "running",
        "started_at": _now_iso(),
        "cmd": cmd_str,
        "work_dir": str(wd),
    }
    if extra_status:
        status.update(extra_status)
    _write_status(wd, status)
    return status


@mcp.tool()
def start_simulation(
    input_script: str,
    num_procs: int = 1,
    name: str | None = None,
) -> dict:
    """Start LIGGGHTS in the background and return a run_id immediately.

    input_script: full text of the LIGGGHTS input deck (written to input.in).
    num_procs:    >1 launches via mpirun -np N.
    name:         optional run_id; auto-generated if omitted.
    """
    run_id = name or uuid.uuid4().hex[:8]
    wd = _run_dir(run_id)
    if wd.exists() and (pid := _read_pid(wd)) and _alive(pid):
        raise RuntimeError(f"run_id {run_id} is already running")
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "input.in").write_text(input_script)

    cmd = [LIGGGHTS_BIN, "-in", "input.in"]
    if num_procs > 1:
        cmd = ["mpirun", "-np", str(num_procs)] + cmd

    return _launch(wd, cmd, run_id=run_id, extra_status={"num_procs": num_procs})


@mcp.tool()
def start_from_file(input_path: str, num_procs: int = 1, name: str | None = None) -> dict:
    """Start a run from an existing input deck file. Copies it into the run dir."""
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(input_path)
    return start_simulation(src.read_text(), num_procs=num_procs, name=name)


@mcp.tool()
def start_from_dir(
    case_dir: str,
    deck_relpath: str,
    name: str | None = None,
    num_procs: int = 1,
    link_mode: str = "copy",
) -> dict:
    """Snapshot a whole case directory into the run dir, then run LIGGGHTS on a deck inside it.

    case_dir:     directory containing all case assets (STL, .liggghts deck,
                  .lammpstrj initial state, case.json, etc.).
    deck_relpath: path to the LIGGGHTS input deck *relative to case_dir*.
    name:         optional run_id; auto-generated if omitted.
    num_procs:    >1 launches via mpirun -np N.
    link_mode:    "copy" (default, isolated full copy) or "symlink"
                  (file-by-file symlinks; outputs still land in run dir, but
                  any pre-existing files of the same name in case_dir would be
                  shadowed by writes — use only for read-only inputs).
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

    run_id = name or uuid.uuid4().hex[:8]
    wd = _run_dir(run_id)
    if wd.exists() and (pid := _read_pid(wd)) and _alive(pid):
        raise RuntimeError(f"run_id {run_id} is already running")
    if wd.exists():
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

    cmd = [LIGGGHTS_BIN, "-in", str(deck_rel)]
    if num_procs > 1:
        cmd = ["mpirun", "-np", str(num_procs)] + cmd

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
        },
    )


@mcp.tool()
def run_plate_case(
    case_dir: str,
    pinn_root: str = "/home/boran/PINN",
    name: str | None = None,
    postprocess: bool = True,
) -> dict:
    """Run the full plate-reference pipeline in-place on a case dir.

    Invokes hpc/liggghts_plate_tier1/run_one_case.sh (settlement → plate_z0
    recompute → intrusion). Writes outputs *into the case dir itself* (the
    bash script does `cd "$case_dir"`), and captures stdout+stderr into the
    MCP run dir's log.run.

    If postprocess=True, also runs scripts/postprocess_liggghts_plate_hpc_sweep.py
    --case-id <basename(case_dir)> after the LIGGGHTS pipeline finishes.

    case_dir:    absolute path to the case directory.
    pinn_root:   PINN repo root (default /home/boran/PINN).
    name:        optional run_id.
    postprocess: also run the sweep postprocessor (default True).
    """
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
    if wd.exists() and (pid := _read_pid(wd)) and _alive(pid):
        raise RuntimeError(f"run_id {run_id} is already running")
    wd.mkdir(parents=True, exist_ok=True)

    # Wrapper script: run the pipeline, then optionally postprocess. Both
    # phases share the same log.run via _launch's stdout redirect.
    postproc = pinn / "scripts" / "postprocess_liggghts_plate_hpc_sweep.py"
    parts = [
        "set -e",
        f'echo "[mcp] phase=run_one_case case_id={case_id}"',
        f'bash {runner} {case}',
    ]
    if postprocess:
        if not postproc.is_file():
            raise FileNotFoundError(f"postprocess script not found: {postproc}")
        parts += [
            f'echo "[mcp] phase=postprocess case_id={case_id}"',
            f'python3 {postproc} --case-id {case_id}',
        ]
    parts.append(f'echo "[mcp] phase=done case_id={case_id}"')
    script = "\n".join(parts)
    (wd / "wrapper.sh").write_text(script)

    cmd = ["bash", str(wd / "wrapper.sh")]
    return _launch(
        wd,
        cmd,
        run_id=run_id,
        cwd=pinn,
        env_extra={
            "LIGGGHTS_BIN": LIGGGHTS_BIN,
            "PINN_ROOT": str(pinn),
        },
        extra_status={
            "kind": "run_plate_case",
            "case_dir": str(case),
            "case_id": case_id,
            "pinn_root": str(pinn),
            "postprocess": postprocess,
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
def list_outputs(run_id: str, patterns: list[str] | None = None) -> list[str]:
    """List output files in a run dir matching the given glob patterns.

    Default patterns cover dumps, trajectories, CSV/JSON summaries, and logs:
    *.dump, *.lammpstrj, *.csv, *.json, *.log, log.*, screen.*, *.vtk, *.vtp,
    *.restart, post/*. Pass `patterns` to override.

    Excludes the MCP server's own bookkeeping files (pid, cmd, status.json,
    log.run, wrapper.sh, input.in).
    """
    wd = _run_dir(run_id)
    pats = patterns or list(_DEFAULT_OUTPUT_PATTERNS)
    bookkeeping = {"pid", "cmd", "status.json", "log.run", "wrapper.sh", "input.in"}
    seen: set[str] = set()
    for pat in pats:
        for p in wd.glob(pat):
            if p.name in bookkeeping:
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
    """Send SIGTERM to a running simulation (SIGKILL if it ignores)."""
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
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    status = _read_status(wd)
    if status:
        status = _finalize_if_done(run_id, wd, status)
        if status.get("status") == "running":
            status["status"] = "finished"
            status["finished_at"] = _now_iso()
            status["exit_code"] = status.get("exit_code", -signal.SIGTERM)
            _write_status(wd, status)
    return {"run_id": run_id, "status": "terminated", "pid": pid}


if __name__ == "__main__":
    mcp.run()
