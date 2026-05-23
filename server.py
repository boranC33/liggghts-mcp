"""LIGGGHTS MCP server — start runs, poll status, read logs, list dumps."""
from mcp.server.fastmcp import FastMCP
import os
import re
import signal
import subprocess
import uuid
from pathlib import Path

mcp = FastMCP("liggghts")

LIGGGHTS_BIN = os.environ.get("LIGGGHTS_BIN", "/usr/local/bin/liggghts")
RUNS = Path(os.environ.get("LIGGGHTS_RUNS", Path.home() / "liggghts_runs"))
RUNS.mkdir(parents=True, exist_ok=True)

_LIB_DIR = Path(__file__).parent / "lib"


def _child_env() -> dict:
    env = os.environ.copy()
    if _LIB_DIR.is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            f"{_LIB_DIR}:{existing}" if existing else str(_LIB_DIR)
        )
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
    if wd.exists() and _read_pid(wd) and _alive(_read_pid(wd)):
        raise RuntimeError(f"run_id {run_id} is already running")
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "input.in").write_text(input_script)

    cmd = [LIGGGHTS_BIN, "-in", "input.in"]
    if num_procs > 1:
        cmd = ["mpirun", "-np", str(num_procs)] + cmd

    log = open(wd / "log.run", "w")
    proc = subprocess.Popen(
        cmd,
        cwd=wd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=_child_env(),
    )
    (wd / "pid").write_text(str(proc.pid))
    (wd / "cmd").write_text(" ".join(cmd))
    _PROCS[run_id] = proc
    return {
        "run_id": run_id,
        "pid": proc.pid,
        "work_dir": str(wd),
        "cmd": " ".join(cmd),
    }


@mcp.tool()
def start_from_file(input_path: str, num_procs: int = 1, name: str | None = None) -> dict:
    """Start a run from an existing input deck file. Copies it into the run dir."""
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(input_path)
    return start_simulation(src.read_text(), num_procs=num_procs, name=name)


@mcp.tool()
def check_status(run_id: str) -> dict:
    """Return running/finished/unknown for a run, plus last log line and exit code."""
    wd = _run_dir(run_id)
    pid = _read_pid(wd)
    if pid is None:
        return {"run_id": run_id, "status": "unknown"}
    log = wd / "log.run"
    last = ""
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()
        last = lines[-1] if lines else ""

    proc = _PROCS.get(run_id)
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return {"run_id": run_id, "status": "running", "pid": pid, "last_log": last}
        return {"run_id": run_id, "status": "finished", "pid": pid,
                "exit_code": rc, "last_log": last}

    if _alive(pid):
        return {"run_id": run_id, "status": "running", "pid": pid, "last_log": last}
    return {"run_id": run_id, "status": "finished", "pid": pid, "last_log": last}


@mcp.tool()
def read_log(run_id: str, tail: int = 200) -> str:
    """Return the last `tail` lines of log.run for a run."""
    wd = _run_dir(run_id)
    log = wd / "log.run"
    if not log.exists():
        return f"no log for {run_id}"
    lines = log.read_text(errors="replace").splitlines()
    return "\n".join(lines[-tail:])


@mcp.tool()
def list_dumps(run_id: str) -> list[str]:
    """List dump/restart/post files produced by a run."""
    wd = _run_dir(run_id)
    out: list[str] = []
    for pat in ("*.dump", "*.vtk", "*.vtp", "*.restart", "post/*"):
        out.extend(str(p) for p in wd.glob(pat))
    return sorted(out)


@mcp.tool()
def list_runs() -> list[dict]:
    """List all known runs and their current status."""
    out = []
    for wd in sorted(RUNS.iterdir()):
        if not wd.is_dir():
            continue
        pid = _read_pid(wd)
        status = "unknown"
        if pid is not None:
            status = "running" if _alive(pid) else "finished"
        out.append({"run_id": wd.name, "status": status, "pid": pid})
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
    return {"run_id": run_id, "status": "terminated", "pid": pid}


if __name__ == "__main__":
    mcp.run()
