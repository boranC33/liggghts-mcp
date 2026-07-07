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
from itertools import product
from pathlib import Path

mcp = FastMCP("liggghts")

__version__ = "0.6.0"

LIGGGHTS_BIN = os.environ.get("LIGGGHTS_BIN", "/usr/local/bin/liggghts")
RUNS = Path(os.environ.get("LIGGGHTS_RUNS", Path.home() / "liggghts_runs"))
RUNS.mkdir(parents=True, exist_ok=True)

_LIB_DIR = Path(__file__).parent / "lib"
_BOOKKEEPING_FILES = {
    "pid",
    "cmd",
    "status.json",
    "log.run",
    "wrapper.sh",
    "input.in",
    "exit_code",
}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off")


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer") from e


def _read_only() -> bool:
    return _env_bool("LIGGGHTS_READ_ONLY", False)


def _ensure_write_allowed(action: str) -> None:
    if _read_only():
        raise PermissionError(
            f"{action} is disabled because LIGGGHTS_READ_ONLY is enabled"
        )


def _ensure_overwrite_allowed(overwrite: bool) -> None:
    if overwrite and not _env_bool("LIGGGHTS_ALLOW_OVERWRITE", False):
        raise PermissionError(
            "overwrite=True is disabled; set LIGGGHTS_ALLOW_OVERWRITE=1 to allow it"
        )


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


def _has_path_separator(value: str) -> bool:
    return os.sep in value or (os.altsep is not None and os.altsep in value)


def _liggghts_executable() -> str:
    """Return the executable string used for subprocess launches.

    Path-like values get `~` expansion. Bare command names are left alone so
    the OS can resolve them through PATH at launch time.
    """
    if _has_path_separator(LIGGGHTS_BIN):
        return str(Path(LIGGGHTS_BIN).expanduser().resolve())
    return LIGGGHTS_BIN


def _resolve_liggghts_executable() -> Path | None:
    """Resolve LIGGGHTS_BIN for validation without changing launch semantics."""
    exe = _liggghts_executable()
    if _has_path_separator(exe):
        p = Path(exe)
        return p if p.is_file() else None
    found = shutil.which(exe)
    return Path(found) if found else None


def _validate_rank_count(value: int, name: str) -> None:
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    max_ranks = _env_int("LIGGGHTS_MAX_RANKS")
    if max_ranks is not None and value > max_ranks:
        raise ValueError(f"{name} must be <= LIGGGHTS_MAX_RANKS ({max_ranks})")


def _max_concurrent_runs() -> int | None:
    value = _env_int("LIGGGHTS_MAX_CONCURRENT_RUNS")
    if value is not None and value < 1:
        raise ValueError("LIGGGHTS_MAX_CONCURRENT_RUNS must be >= 1")
    return value


def _max_sweep_cases() -> int:
    value = _env_int("LIGGGHTS_MAX_SWEEP_CASES", 32)
    if value is None or value < 1:
        raise ValueError("LIGGGHTS_MAX_SWEEP_CASES must be >= 1")
    return value


def _max_read_bytes() -> int:
    value = _env_int("LIGGGHTS_MAX_READ_BYTES", 256 * 1024)
    if value is None or value < 1:
        raise ValueError("LIGGGHTS_MAX_READ_BYTES must be >= 1")
    return value


def _vis_timeout() -> int:
    value = _env_int("LIGGGHTS_VIS_TIMEOUT", 300)
    if value is None or value < 1:
        raise ValueError("LIGGGHTS_VIS_TIMEOUT must be >= 1")
    return value


def _allowed_case_roots() -> list[Path]:
    raw = os.environ.get("LIGGGHTS_ALLOWED_CASE_ROOTS", "")
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser().resolve())
    return roots


def _ensure_allowed_case_path(path: Path) -> None:
    roots = _allowed_case_roots()
    if not roots:
        return
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    allowed = ", ".join(str(root) for root in roots)
    raise PermissionError(f"path is outside LIGGGHTS_ALLOWED_CASE_ROOTS: {allowed}")


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
    return _mpi_prefix(mpi_ranks, mpirun) + [_liggghts_executable()] + args


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


def _process_group_alive(pgid: int) -> bool:
    """True iff at least one non-zombie process remains in a process group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return True
    for proc_dir in proc_root.iterdir():
        if not proc_dir.name.isdigit():
            continue
        member_pid = int(proc_dir.name)
        try:
            if os.getpgid(member_pid) == pgid and _alive(member_pid):
                return True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return False


def _wait_until_group_not_alive(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_group_alive(pgid):
            return True
        time.sleep(0.1)
    return not _process_group_alive(pgid)


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


def _validate_output_pattern(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise TypeError("output patterns must be strings")
    if not pattern:
        raise ValueError("output patterns must not be empty")
    p = Path(pattern)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("output patterns must be relative and stay inside the run dir")
    return pattern


def _stays_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _running_run_count() -> int:
    count = 0
    if not RUNS.exists():
        return count
    for wd in RUNS.iterdir():
        if not wd.is_dir():
            continue
        status = _read_status(wd)
        if not status:
            pid = _read_pid(wd)
            if pid is not None and _alive(pid):
                count += 1
            continue
        status = _finalize_if_done(wd.name, wd, status)
        if status.get("status") == "running":
            count += 1
    return count


def _ensure_concurrency_capacity(new_runs: int = 1) -> None:
    max_runs = _max_concurrent_runs()
    if max_runs is None:
        return
    running = _running_run_count()
    if running + new_runs > max_runs:
        raise RuntimeError(
            f"starting {new_runs} run(s) would exceed "
            f"LIGGGHTS_MAX_CONCURRENT_RUNS ({max_runs}); currently running: {running}"
        )


def _prepare_run_dir(run_id: str, overwrite: bool) -> Path:
    _ensure_overwrite_allowed(overwrite)
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
    return wd


def _validate_relpath(value: str, label: str = "path") -> Path:
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or str(rel) in ("", "."):
        raise ValueError(f"{label} must be relative and stay inside the run dir")
    return rel


def _resolve_run_path(run_id: str, relpath: str) -> Path:
    wd = _run_dir(run_id)
    rel = _validate_relpath(relpath, "relpath")
    path = (wd / rel).resolve()
    if not _stays_inside(path, wd):
        raise ValueError("relpath escapes run dir")
    return path


def _prepare_run_output_path(run_id: str, relpath: str, overwrite: bool = False) -> Path:
    path = _resolve_run_path(run_id, relpath)
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {relpath}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_run_text(run_id: str, relpath: str, max_bytes: int | None = None) -> str:
    path = _resolve_run_path(run_id, relpath)
    if not path.is_file():
        raise FileNotFoundError(f"file not found in run {run_id}: {relpath}")
    limit = max_bytes if max_bytes is not None else _max_read_bytes()
    if limit < 1:
        raise ValueError("max_bytes must be >= 1")
    with path.open("rb") as f:
        data = f.read(limit + 1)
    return data[:limit].decode("utf-8", errors="replace")


def _duration_seconds(status: dict) -> float | None:
    started = status.get("started_at")
    finished = status.get("finished_at")
    if not started:
        return None
    try:
        start_dt = datetime.fromisoformat(started)
        end_dt = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
        return max(0.0, (end_dt - start_dt).total_seconds())
    except (TypeError, ValueError):
        return None


def _deck_code_line(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _deck_command(line: str) -> str:
    code = _deck_code_line(line)
    return code.split(None, 1)[0].lower() if code else ""


def _deck_findings(input_script: str) -> dict:
    lines = input_script.splitlines()
    commands: dict[str, int] = {}
    warnings: list[str] = []
    errors: list[str] = []
    run_steps: list[int] = []
    shell_lines: list[int] = []
    include_lines: list[int] = []

    for lineno, line in enumerate(lines, start=1):
        code = _deck_code_line(line)
        if not code:
            continue
        parts = code.split()
        cmd = parts[0].lower()
        commands[cmd] = commands.get(cmd, 0) + 1
        if cmd == "shell":
            shell_lines.append(lineno)
        if cmd in {"include", "jump"}:
            include_lines.append(lineno)
            if len(parts) > 1:
                path_arg = parts[1]
                p = Path(path_arg)
                if p.is_absolute() or ".." in p.parts:
                    warnings.append(
                        f"line {lineno}: {cmd} references a path outside the deck directory"
                    )
        if cmd == "run" and len(parts) > 1:
            try:
                run_steps.append(int(float(parts[1])))
            except ValueError:
                warnings.append(f"line {lineno}: run step count is not a literal number")

    if shell_lines:
        msg = "shell command(s) present at line(s): " + ", ".join(map(str, shell_lines))
        if _env_bool("LIGGGHTS_ALLOW_DECK_SHELL", False):
            warnings.append(msg)
        else:
            errors.append(msg + "; set LIGGGHTS_ALLOW_DECK_SHELL=1 to allow")
    if not input_script.strip():
        errors.append("deck is empty")
    for required in ("units", "atom_style"):
        if required not in commands:
            warnings.append(f"missing common LIGGGHTS command: {required}")
    if "run" not in commands and "minimize" not in commands:
        warnings.append("deck has no run or minimize command")

    return {
        "line_count": len(lines),
        "input_bytes": len(input_script.encode("utf-8")),
        "commands": commands,
        "run_steps": run_steps,
        "total_literal_run_steps": sum(run_steps) if run_steps else None,
        "has_mesh": any(cmd.startswith("mesh") or "mesh" in cmd for cmd in commands),
        "has_dump": "dump" in commands,
        "has_restart": "restart" in commands or "write_restart" in commands,
        "has_read_data": "read_data" in commands,
        "has_shell": bool(shell_lines),
        "has_include_or_jump": bool(include_lines),
        "warnings": warnings,
        "errors": errors,
    }


def _validate_deck_safety(input_script: str) -> None:
    findings = _deck_findings(input_script)
    if findings["errors"]:
        raise PermissionError("; ".join(findings["errors"]))


def _estimate_deck(input_script: str, num_procs: int = 1) -> dict:
    _validate_rank_count(num_procs, "num_procs")
    findings = _deck_findings(input_script)
    cpu_count = os.cpu_count() or 1
    warnings = list(findings["warnings"])
    if num_procs > cpu_count:
        warnings.append(
            f"num_procs ({num_procs}) exceeds detected CPU count ({cpu_count})"
        )
    max_ranks = _env_int("LIGGGHTS_MAX_RANKS")
    return {
        **findings,
        "num_procs": num_procs,
        "detected_cpu_count": cpu_count,
        "max_ranks": max_ranks,
        "warnings": warnings,
        "ok": not findings["errors"],
    }


def _slugify(value: str, fallback: str = "run") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    if not slug:
        slug = fallback
    return slug[:64]


def _render_template(template: str, values: dict) -> str:
    rendered = template
    for key, value in values.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(key)):
            raise ValueError(f"invalid template parameter name: {key!r}")
        rendered = rendered.replace("{{" + str(key) + "}}", str(value))
    leftover = re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}", rendered)
    if leftover:
        raise ValueError("unresolved template parameter(s): " + ", ".join(sorted(set(leftover))))
    return rendered


def _parameter_combinations(parameters: dict) -> list[dict]:
    if not parameters:
        raise ValueError("parameters must not be empty")
    keys = list(parameters)
    values: list[list] = []
    for key in keys:
        items = parameters[key]
        if not isinstance(items, list) or not items:
            raise ValueError(f"parameters[{key!r}] must be a non-empty list")
        values.append(items)
    combos = [dict(zip(keys, row)) for row in product(*values)]
    max_cases = _max_sweep_cases()
    if len(combos) > max_cases:
        raise ValueError(
            f"parameter sweep has {len(combos)} cases; "
            f"LIGGGHTS_MAX_SWEEP_CASES is {max_cases}"
        )
    return combos


def _copy_run_tree_for_clone(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            lower = name.lower()
            if name in (_BOOKKEEPING_FILES - {"input.in"}):
                ignored.add(name)
            elif lower.startswith("log.") or lower.startswith("screen."):
                ignored.add(name)
            elif Path(name).suffix.lower() in {
                ".dump",
                ".lammpstrj",
                ".vtk",
                ".vtp",
                ".vtu",
                ".restart",
            }:
                ignored.add(name)
            elif name == "post":
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


def _resolve_executable(env_name: str, candidates: list[str]) -> str | None:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        exe = str(Path(configured).expanduser().resolve()) if _has_path_separator(configured) else configured
        return exe if shutil.which(exe) or Path(exe).is_file() else None
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _command_probe(cmd: list[str], timeout: int = 10) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_child_env(),
        )
        blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        first_line = next((line.strip() for line in blob.splitlines() if line.strip()), "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "version_line": first_line,
            "error_tail": "" if proc.returncode == 0 else blob[-2048:],
        }
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "exit_code": -1, "version_line": "", "error_tail": str(e)}


def _python_import_probe(python_bin: str, module: str, version_expr: str) -> dict:
    code = f"import {module}; print({version_expr})"
    return _command_probe([python_bin, "-c", code])


def _ovito_python() -> str | None:
    configured = os.environ.get("OVITO_PYTHON", "").strip()
    if configured:
        exe = str(Path(configured).expanduser().resolve()) if _has_path_separator(configured) else configured
        return exe if shutil.which(exe) or Path(exe).is_file() else None
    for candidate in ("python3", "python"):
        exe = shutil.which(candidate)
        if not exe:
            continue
        if _python_import_probe(exe, "ovito", "ovito.version_string")["ok"]:
            return exe
    return None


def _paraview_python(use_pvbatch: bool = False) -> str | None:
    env_name = "PVBATCH_BIN" if use_pvbatch else "PVPYTHON_BIN"
    configured = os.environ.get(env_name, "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(
            str(Path(configured).expanduser().resolve())
            if _has_path_separator(configured)
            else configured
        )
    candidates.extend(["pvbatch"] if use_pvbatch else ["pvpython"])
    candidates.extend(["python3", "python"])

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        exe = candidate if _has_path_separator(candidate) else shutil.which(candidate)
        if not exe:
            continue
        if _python_import_probe(str(exe), "paraview.simple", "'paraview.simple'")["ok"]:
            return str(exe)
    return None


def _write_helper_script(run_id: str, name: str, content: str) -> Path:
    scripts_dir = _resolve_run_path(run_id, "visualization/scripts")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    path = scripts_dir / name
    path.write_text(content)
    return path


def _run_visual_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout or _vis_timeout(),
            stdin=subprocess.DEVNULL,
            env=_child_env(),
        )
        blob = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "cmd": shlex.join(cmd),
            "output_tail": blob[-4096:],
        }
    except subprocess.TimeoutExpired as e:
        blob = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""
        return {
            "ok": False,
            "exit_code": -1,
            "duration_seconds": round(time.monotonic() - started, 3),
            "cmd": shlex.join(cmd),
            "output_tail": f"TIMEOUT after {timeout or _vis_timeout()}s\n" + (blob[-4096:] if blob else ""),
        }
    except OSError as e:
        return {
            "ok": False,
            "exit_code": -1,
            "duration_seconds": round(time.monotonic() - started, 3),
            "cmd": shlex.join(cmd),
            "output_tail": f"failed to spawn command: {e}",
        }


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
        proc = _PROCS.get(run_id)
        if proc is not None:
            proc.poll()
    status["status"] = "finished"
    status["exit_code"] = rc
    status["finished_at"] = _now_iso()
    _write_status(wd, status)
    _PROCS.pop(run_id, None)
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
    try:
        proc = subprocess.Popen(
            spawn_cmd,
            cwd=cwd or wd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_child_env(env_extra),
        )
    finally:
        log.close()
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
        "liggghts_executable": _liggghts_executable(),
        "liggghts_resolved_path": None,
        "runs_root": str(RUNS),
        "read_only": _read_only(),
        "max_ranks": _env_int("LIGGGHTS_MAX_RANKS"),
        "max_concurrent_runs": _max_concurrent_runs(),
        "allowed_case_roots": [str(root) for root in _allowed_case_roots()],
        "exists": False,
        "executable": False,
        "version_line": None,
        "mesh_surface": None,
        "mesh_surface_stress": None,
        "mesh_surface_stress_probe": None,
        **_mpi_status_fields(mpi_ranks, mpirun),
    }
    p = _resolve_liggghts_executable()
    info["liggghts_resolved_path"] = str(p) if p is not None else None
    info["exists"] = p is not None
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
def estimate_case_cost(input_script: str, num_procs: int = 1) -> dict:
    """Return a static, conservative estimate/lint report for a LIGGGHTS deck."""
    return _estimate_deck(input_script, num_procs=num_procs)


@mcp.tool()
def validate_deck(
    input_script: str,
    execute: bool = False,
    timeout: int = 30,
    num_procs: int = 1,
    mpirun: str = "mpirun",
) -> dict:
    """Validate a deck statically, and optionally execute it in a tempdir.

    The optional execution is not a true parse-only mode: LIGGGHTS will run the
    provided deck. Keep decks tiny, e.g. `run 0`, when using execute=True.
    """
    _validate_rank_count(num_procs, "num_procs")
    if timeout < 1:
        raise ValueError("timeout must be >= 1")
    max_timeout = _env_int("LIGGGHTS_VALIDATE_TIMEOUT_MAX", 120)
    if max_timeout is not None and timeout > max_timeout:
        raise ValueError(
            f"timeout exceeds LIGGGHTS_VALIDATE_TIMEOUT_MAX ({max_timeout})"
        )
    result = _estimate_deck(input_script, num_procs=num_procs)
    result["execute"] = execute
    if not execute:
        return result
    if result["errors"]:
        result.update(
            {
                "execute_ok": False,
                "execute_exit_code": None,
                "execute_error_tail": "deck failed static safety validation",
            }
        )
        return result

    tmpdir = Path(tempfile.mkdtemp(prefix="liggghts_deck_validate_"))
    result["execute_work_dir"] = str(tmpdir)
    (tmpdir / "input.in").write_text(input_script)
    try:
        proc = subprocess.run(
            _liggghts_cmd(["-in", "input.in"], num_procs, mpirun),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=str(tmpdir),
            env=_child_env(),
            stdin=subprocess.DEVNULL,
        )
        blob = (proc.stdout or "") + (proc.stderr or "")
        result["execute_exit_code"] = proc.returncode
        result["execute_ok"] = proc.returncode == 0
        result["execute_error_tail"] = "" if proc.returncode == 0 else blob[-4096:]
    except subprocess.TimeoutExpired as e:
        blob = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""
        result["execute_exit_code"] = -1
        result["execute_ok"] = False
        result["execute_error_tail"] = (
            f"TIMEOUT after {timeout}s\n" + (blob[-4096:] if blob else "")
        )
    except OSError as e:
        result["execute_exit_code"] = -1
        result["execute_ok"] = False
        result["execute_error_tail"] = f"failed to spawn liggghts command: {e}"
    return result


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
                  starting, but only when LIGGGHTS_ALLOW_OVERWRITE=1. A
                  still-running prior run is always rejected regardless of
                  this flag.
    mpirun:       MPI launcher executable/options. The server appends
                  `-np num_procs` when num_procs > 1.
    """
    _ensure_write_allowed("start_simulation")
    _validate_rank_count(num_procs, "num_procs")
    _validate_deck_safety(input_script)
    _ensure_concurrency_capacity()
    run_id = name or uuid.uuid4().hex[:8]
    wd = _prepare_run_dir(run_id, overwrite)
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
def start_parameter_sweep(
    template: str,
    parameters: dict,
    name_prefix: str | None = None,
    num_procs: int = 1,
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Render `{{param}}` template cases and start one run per parameter set."""
    _ensure_write_allowed("start_parameter_sweep")
    _validate_rank_count(num_procs, "num_procs")
    combos = _parameter_combinations(parameters)
    _ensure_concurrency_capacity(len(combos))
    prefix = _slugify(name_prefix or f"sweep_{uuid.uuid4().hex[:8]}", "sweep")
    sweep_id = prefix
    runs = []
    for index, values in enumerate(combos, start=1):
        deck = _render_template(template, values)
        run_id = _slugify(f"{prefix}_{index:03d}", f"{prefix}_{index:03d}")
        status = start_simulation(
            deck,
            num_procs=num_procs,
            name=run_id,
            overwrite=overwrite,
            mpirun=mpirun,
        )
        wd = _run_dir(run_id)
        persisted = _read_status(wd)
        persisted.update(
            {
                "kind": "start_parameter_sweep",
                "sweep_id": sweep_id,
                "case_index": index,
                "parameters": values,
            }
        )
        _write_status(wd, persisted)
        status.update(persisted)
        runs.append(status)
    return {"sweep_id": sweep_id, "case_count": len(runs), "runs": runs}


@mcp.tool()
def start_from_file(
    input_path: str,
    num_procs: int = 1,
    name: str | None = None,
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Start a run from an existing input deck file. Copies it into the run dir."""
    _ensure_write_allowed("start_from_file")
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(input_path)
    _ensure_allowed_case_path(src)
    deck_text = src.read_text()
    _validate_deck_safety(deck_text)
    return start_simulation(
        deck_text, num_procs=num_procs, name=name, overwrite=overwrite, mpirun=mpirun
    )


@mcp.tool()
def clone_run(
    run_id: str,
    new_name: str,
    replacements: dict[str, str] | None = None,
    append_text: str = "",
    deck_relpath: str = "input.in",
    num_procs: int = 1,
    overwrite: bool = False,
    mpirun: str = "mpirun",
) -> dict:
    """Clone an existing run directory, edit its deck, and start the clone."""
    _ensure_write_allowed("clone_run")
    _validate_rank_count(num_procs, "num_procs")
    _ensure_concurrency_capacity()
    src = _run_dir(run_id)
    if not src.is_dir():
        raise FileNotFoundError(f"run not found: {run_id}")
    dst = _prepare_run_dir(new_name, overwrite)
    _copy_run_tree_for_clone(src, dst)
    deck_rel = _validate_relpath(deck_relpath, "deck_relpath")
    deck = dst / deck_rel
    if not deck.is_file():
        raise FileNotFoundError(f"deck not found in cloned run: {deck_relpath}")
    text = deck.read_text()
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    if append_text:
        text = text.rstrip() + "\n" + append_text.lstrip()
    _validate_deck_safety(text)
    deck.write_text(text)

    cmd = _liggghts_cmd(["-in", str(deck_rel)], num_procs, mpirun)
    return _launch(
        dst,
        cmd,
        run_id=new_name,
        extra_status={
            "kind": "clone_run",
            "source_run_id": run_id,
            "deck_relpath": str(deck_rel),
            "num_procs": num_procs,
            "mpi_ranks": num_procs,
            **_mpi_status_fields(num_procs, mpirun),
        },
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
                  re-snapshotting, but only when LIGGGHTS_ALLOW_OVERWRITE=1.
                  A still-running prior run is always rejected regardless of
                  this flag.
    """
    _ensure_write_allowed("start_from_dir")
    src = Path(case_dir).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"case_dir not a directory: {case_dir}")
    _ensure_allowed_case_path(src)
    deck_rel = Path(deck_relpath)
    if deck_rel.is_absolute() or ".." in deck_rel.parts:
        raise ValueError("deck_relpath must be relative and stay inside case_dir")
    if not (src / deck_rel).is_file():
        raise FileNotFoundError(f"deck not found: {src / deck_rel}")
    _validate_deck_safety((src / deck_rel).read_text())
    if link_mode not in ("copy", "symlink"):
        raise ValueError("link_mode must be 'copy' or 'symlink'")
    _validate_rank_count(num_procs, "num_procs")
    _ensure_concurrency_capacity()

    run_id = name or uuid.uuid4().hex[:8]
    wd = _prepare_run_dir(run_id, overwrite)

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
    if tail < 0:
        raise ValueError("tail must be >= 0")
    wd = _run_dir(run_id)
    log = wd / "log.run"
    if not log.exists():
        return f"no log for {run_id}"
    if tail == 0:
        return ""
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
    "*.vtu",
    "*.restart",
    "post/*",
    "visualization/*",
    "visualization/**/*",
    "converted/*",
    "artifacts/*",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.mp4",
)


_SIM_OUTPUT_PATTERNS = (
    "*.dump",
    "*.lammpstrj",
    "*.csv",
    "*.json",
    "*.log",
    "log.*",
    "screen.*",
    "*.vtk",
    "*.vtp",
    "*.vtu",
    "*.restart",
    "post/*",
)


_VISUAL_OUTPUT_PATTERNS = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.mp4",
    "*.avi",
    "*.mov",
    "visualization/*",
    "visualization/**/*",
    "converted/*",
    "converted/**/*",
)


def _classify_output(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in _BOOKKEEPING_FILES:
        return "bookkeeping"
    if name.startswith("log.") or suffix == ".log":
        return "log"
    if suffix in {".dump", ".lammpstrj"}:
        return "dump"
    if suffix in {".vtk", ".vtp", ".vtu"}:
        return "vtk"
    if suffix == ".restart" or "restart" in name:
        return "restart"
    if suffix in {".png", ".jpg", ".jpeg"}:
        return "image"
    if suffix in {".mp4", ".avi", ".mov"}:
        return "animation"
    if suffix in {".csv", ".json"}:
        return "summary"
    if "visualization" in path.parts:
        return "visualization"
    if "artifacts" in path.parts:
        return "artifact"
    return "output"


def _file_record(path: Path, root: Path) -> dict:
    st = path.stat()
    try:
        relpath = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        relpath = str(path.relative_to(root))
    return {
        "path": str(path),
        "relpath": relpath,
        "kind": _classify_output(path),
        "size_bytes": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def _output_records(
    run_id: str,
    patterns: list[str] | None = None,
    include_bookkeeping: bool = False,
) -> list[dict]:
    wd = _run_dir(run_id)
    pats = list(_DEFAULT_OUTPUT_PATTERNS) if patterns is None else patterns
    seen: dict[str, dict] = {}
    for pat in pats:
        pat = _validate_output_pattern(pat)
        for p in wd.glob(pat):
            if not p.is_file():
                continue
            if not _stays_inside(p, wd):
                continue
            if not include_bookkeeping and p.name in _BOOKKEEPING_FILES:
                continue
            record = _file_record(p, wd)
            seen[record["relpath"]] = record
    return [seen[key] for key in sorted(seen)]


def _read_last_bytes(path: Path, max_bytes: int) -> tuple[str, bool]:
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            data = f.read(max_bytes)
            return data.decode("utf-8", errors="replace"), True
        data = f.read()
        return data.decode("utf-8", errors="replace"), False


def _maybe_number(value: str) -> int | float | str:
    try:
        number = float(value)
    except ValueError:
        return value
    if number.is_integer():
        return int(number)
    return number


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


_LOOP_RE = re.compile(
    r"Loop time of\s+([0-9.eE+-]+)\s+on\s+(\d+)\s+procs\s+for\s+(\d+)\s+steps\s+with\s+(\d+)\s+atoms"
)


def _parse_log_text(text: str, truncated: bool = False) -> dict:
    lines = text.splitlines()
    warnings: list[str] = []
    errors: list[str] = []
    last_thermo: dict | None = None
    thermo_header: list[str] | None = None
    loop_summary: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("WARNING") or " WARNING:" in upper:
            warnings.append(stripped)
        if upper.startswith("ERROR") or " ERROR:" in upper:
            errors.append(stripped)
        parts = stripped.split()
        if parts and parts[0].lower() == "step":
            thermo_header = parts
            continue
        if thermo_header and len(parts) >= len(thermo_header):
            values = parts[: len(thermo_header)]
            if values and _is_number(values[0]):
                last_thermo = {
                    key: _maybe_number(value)
                    for key, value in zip(thermo_header, values)
                }
                continue
        m = _LOOP_RE.search(stripped)
        if m:
            loop_summary = {
                "loop_time_seconds": float(m.group(1)),
                "procs": int(m.group(2)),
                "steps": int(m.group(3)),
                "atoms": int(m.group(4)),
            }

    return {
        "line_count": len(lines),
        "truncated": truncated,
        "warning_count": len(warnings),
        "error_count": len(errors),
        "recent_warnings": warnings[-20:],
        "recent_errors": errors[-20:],
        "last_thermo": last_thermo,
        "loop_summary": loop_summary,
    }


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
    return [
        record["path"]
        for record in _output_records(run_id, patterns, include_bookkeeping)
    ]


@mcp.tool()
def list_output_details(
    run_id: str,
    patterns: list[str] | None = None,
    include_bookkeeping: bool = False,
) -> list[dict]:
    """List output files with relpath, kind, size, and modified timestamp."""
    return _output_records(run_id, patterns, include_bookkeeping)


@mcp.tool()
def list_dumps(run_id: str) -> list[str]:
    """List dump/restart/post files (alias for list_outputs with dump-only patterns)."""
    return list_outputs(run_id, patterns=["*.dump", "*.vtk", "*.vtp", "*.restart", "post/*"])


@mcp.tool()
def read_output(
    run_id: str,
    relpath: str,
    offset: int = 0,
    max_bytes: int | None = None,
) -> dict:
    """Read a bounded UTF-8 text window from a file inside a run directory."""
    if offset < 0:
        raise ValueError("offset must be >= 0")
    limit = max_bytes if max_bytes is not None else _max_read_bytes()
    if limit < 1:
        raise ValueError("max_bytes must be >= 1")
    configured_limit = _max_read_bytes()
    if limit > configured_limit:
        raise ValueError(
            f"max_bytes exceeds LIGGGHTS_MAX_READ_BYTES ({configured_limit})"
        )
    path = _resolve_run_path(run_id, relpath)
    if not path.is_file():
        raise FileNotFoundError(f"file not found in run {run_id}: {relpath}")
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(min(offset, size))
        data = f.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    return {
        "run_id": run_id,
        "relpath": str(_validate_relpath(relpath, "relpath")),
        "size_bytes": size,
        "offset": offset,
        "bytes_read": len(data),
        "truncated": truncated or offset + len(data) < size,
        "content": data.decode("utf-8", errors="replace"),
    }


@mcp.tool()
def parse_log(run_id: str, max_bytes: int | None = None) -> dict:
    """Parse warnings, errors, final thermo row, and loop summary from log.run."""
    limit = max_bytes if max_bytes is not None else _max_read_bytes()
    if limit < 1:
        raise ValueError("max_bytes must be >= 1")
    configured_limit = _max_read_bytes()
    if limit > configured_limit:
        raise ValueError(
            f"max_bytes exceeds LIGGGHTS_MAX_READ_BYTES ({configured_limit})"
        )
    wd = _run_dir(run_id)
    log = wd / "log.run"
    if not log.exists():
        return {"run_id": run_id, "exists": False}
    text, truncated = _read_last_bytes(log, limit)
    return {
        "run_id": run_id,
        "exists": True,
        "size_bytes": log.stat().st_size,
        **_parse_log_text(text, truncated=truncated),
    }


@mcp.tool()
def summarize_run(run_id: str, max_outputs: int = 20) -> dict:
    """Return a compact status/log/output summary for one run."""
    if max_outputs < 0:
        raise ValueError("max_outputs must be >= 0")
    status = check_status(run_id)
    outputs = list_output_details(run_id)
    total_output_bytes = sum(record["size_bytes"] for record in outputs)
    return {
        "run_id": run_id,
        "status": status.get("status", "unknown"),
        "exit_code": status.get("exit_code"),
        "pid": status.get("pid"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "duration_seconds": _duration_seconds(status),
        "last_log": status.get("last_log", ""),
        "log": parse_log(run_id),
        "output_count": len(outputs),
        "total_output_bytes": total_output_bytes,
        "outputs": outputs[:max_outputs],
        "outputs_truncated": len(outputs) > max_outputs,
    }


@mcp.tool()
def compare_runs(run_ids: list[str]) -> list[dict]:
    """Compare compact summaries for multiple runs."""
    if not run_ids:
        return []
    return [
        {
            "run_id": summary["run_id"],
            "status": summary["status"],
            "exit_code": summary["exit_code"],
            "duration_seconds": summary["duration_seconds"],
            "warning_count": summary["log"].get("warning_count"),
            "error_count": summary["log"].get("error_count"),
            "last_thermo": summary["log"].get("last_thermo"),
            "output_count": summary["output_count"],
            "total_output_bytes": summary["total_output_bytes"],
        }
        for summary in (summarize_run(run_id, max_outputs=0) for run_id in run_ids)
    ]


def _insert_before_run_command(input_script: str, snippet_lines: list[str]) -> str:
    lines = input_script.rstrip().splitlines()
    insert_at = len(lines)
    for idx, line in enumerate(lines):
        if _deck_command(line) in {"run", "minimize"}:
            insert_at = idx
            break
    updated = lines[:insert_at] + snippet_lines + lines[insert_at:]
    return "\n".join(updated).rstrip() + "\n"


@mcp.tool()
def ensure_standard_outputs(
    input_script: str = "",
    run_id: str | None = None,
    deck_relpath: str = "input.in",
    write_back: bool = False,
    dump_every: int = 1000,
    dump_filename: str = "particles.lammpstrj",
    thermo_every: int = 1000,
    add_vtk: bool = False,
    vtk_every: int = 1000,
    vtk_filename: str = "particles_*.vtk",
) -> dict:
    """Add common thermo/dump/optional-VTK output commands to a deck."""
    if dump_every < 1 or thermo_every < 1 or vtk_every < 1:
        raise ValueError("dump_every, thermo_every, and vtk_every must be >= 1")
    if write_back:
        _ensure_write_allowed("ensure_standard_outputs")
    if run_id is not None:
        deck_path = _resolve_run_path(run_id, deck_relpath)
        if not deck_path.is_file():
            raise FileNotFoundError(f"deck not found: {deck_relpath}")
        input_script = deck_path.read_text()
    if not input_script.strip():
        raise ValueError("input_script is empty and no run deck was provided")

    findings = _deck_findings(input_script)
    inserted: list[str] = []
    snippet: list[str] = []
    warnings: list[str] = []

    if "thermo" not in findings["commands"]:
        snippet.append(f"thermo          {thermo_every}")
        inserted.append("thermo")
    if "thermo_style" not in findings["commands"]:
        snippet.append("thermo_style    custom step atoms ke")
        inserted.append("thermo_style")
    if "dump" not in findings["commands"]:
        _validate_relpath(dump_filename, "dump_filename")
        snippet.extend(
            [
                f"dump            mcp_dump all custom {dump_every} {dump_filename} id type x y z vx vy vz radius",
                "dump_modify     mcp_dump sort id",
            ]
        )
        inserted.append("dump")
    if add_vtk:
        _validate_relpath(vtk_filename, "vtk_filename")
        if "dump" in findings["commands"]:
            warnings.append("deck already has dump commands; optional VTK dump was not inserted")
        else:
            snippet.append(
                f"dump            mcp_vtk all vtk {vtk_every} {vtk_filename} id type x y z vx vy vz radius"
            )
            inserted.append("vtk_dump")

    updated = input_script if not snippet else _insert_before_run_command(input_script, snippet)
    if write_back:
        if run_id is None:
            raise ValueError("write_back=True requires run_id")
        deck_path.write_text(updated)
    suggested = [dump_filename, "log.run"]
    if add_vtk:
        suggested.append(vtk_filename)
    return {
        "changed": bool(snippet),
        "inserted": inserted,
        "warnings": warnings,
        "updated_input_script": updated,
        "suggested_output_patterns": suggested,
    }


@mcp.tool()
def validate_visualization_tools() -> dict:
    """Check available OVITO and ParaView command-line tooling."""
    ovito_bin = _resolve_executable("OVITO_BIN", ["ovito"])
    ovito_python = _ovito_python()
    paraview_bin = _resolve_executable("PARAVIEW_BIN", ["paraview"])
    pvpython_bin = _resolve_executable("PVPYTHON_BIN", ["pvpython"])
    pvbatch_bin = _resolve_executable("PVBATCH_BIN", ["pvbatch"])

    return {
        "ovito": {
            "bin": ovito_bin,
            "probe": _command_probe([ovito_bin, "--version"]) if ovito_bin else None,
        },
        "ovito_python": {
            "bin": ovito_python,
            "probe": _python_import_probe(ovito_python, "ovito", "ovito.version_string")
            if ovito_python
            else None,
        },
        "paraview": {
            "bin": paraview_bin,
            "probe": _command_probe([paraview_bin, "--version"]) if paraview_bin else None,
        },
        "pvpython": {
            "bin": pvpython_bin,
            "probe": _command_probe([pvpython_bin, "--version"]) if pvpython_bin else None,
            "python_import_probe": _python_import_probe(
                pvpython_bin, "paraview.simple", "'paraview.simple'"
            )
            if pvpython_bin
            else None,
        },
        "pvbatch": {
            "bin": pvbatch_bin,
            "probe": _command_probe([pvbatch_bin, "--version"]) if pvbatch_bin else None,
            "python_import_probe": _python_import_probe(
                pvbatch_bin, "paraview.simple", "'paraview.simple'"
            )
            if pvbatch_bin
            else None,
        },
        "paraview_python": {
            "bin": _paraview_python(False),
            "probe": _python_import_probe(
                _paraview_python(False), "paraview.simple", "'paraview.simple'"
            )
            if _paraview_python(False)
            else None,
        },
    }


@mcp.tool()
def collect_run_artifacts(
    run_id: str,
    patterns: list[str] | None = None,
    artifact_dir: str = "artifacts",
    mode: str = "copy",
    overwrite: bool = False,
) -> dict:
    """Collect simulation and visualization outputs into a run-local artifact dir."""
    _ensure_write_allowed("collect_run_artifacts")
    if mode not in {"copy", "symlink"}:
        raise ValueError("mode must be 'copy' or 'symlink'")
    wd = _run_dir(run_id)
    artifact_root = _resolve_run_path(run_id, artifact_dir)
    if artifact_root.exists() and not artifact_root.is_dir():
        raise NotADirectoryError(artifact_dir)
    if artifact_root.exists() and overwrite:
        shutil.rmtree(artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    pats = patterns or list(_SIM_OUTPUT_PATTERNS + _VISUAL_OUTPUT_PATTERNS)
    records = _output_records(run_id, pats, include_bookkeeping=False)
    collected: list[dict] = []
    for record in records:
        src = Path(record["path"])
        if _stays_inside(src, artifact_root):
            continue
        dest = artifact_root / record["relpath"]
        if dest.exists():
            if not overwrite:
                continue
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copy2(src, dest)
        else:
            dest.symlink_to(src.resolve())
        collected.append({**record, "artifact_relpath": str(dest.relative_to(wd))})
    manifest = {
        "run_id": run_id,
        "artifact_dir": str(artifact_root.relative_to(wd)),
        "mode": mode,
        "collected_count": len(collected),
        "collected": collected,
        "created_at": _now_iso(),
    }
    (artifact_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _ovito_export_format(output_relpath: str, export_format: str | None) -> str:
    if export_format:
        return export_format
    suffix = Path(output_relpath).suffix.lower()
    if suffix == ".xyz":
        return "xyz"
    if suffix in {".dump", ".lammpstrj"}:
        return "lammps/dump"
    if suffix == ".vtk":
        return "vtk/trimesh"
    raise ValueError("export_format is required for this output extension")


_OVITO_CONVERT_SCRIPT = r"""
from ovito.io import import_file, export_file
import sys

input_path, output_path, export_format = sys.argv[1:4]
pipeline = import_file(input_path)
kwargs = {}
if export_format == "xyz":
    kwargs["columns"] = [
        "Particle Identifier",
        "Particle Type",
        "Position.X",
        "Position.Y",
        "Position.Z",
    ]
try:
    export_file(pipeline, output_path, export_format, multiple_frames=True, **kwargs)
except TypeError:
    export_file(pipeline, output_path, export_format, **kwargs)
"""


@mcp.tool()
def convert_dump_with_ovito(
    run_id: str,
    input_relpath: str,
    output_relpath: str | None = None,
    export_format: str | None = None,
    overwrite: bool = False,
    timeout: int | None = None,
) -> dict:
    """Convert a LIGGGHTS dump/trajectory using the OVITO Python module."""
    _ensure_write_allowed("convert_dump_with_ovito")
    ovito_python = _ovito_python()
    if not ovito_python:
        raise FileNotFoundError("OVITO Python module not found; set OVITO_PYTHON")
    input_path = _resolve_run_path(run_id, input_relpath)
    if not input_path.is_file():
        raise FileNotFoundError(input_relpath)
    if output_relpath is None:
        output_relpath = f"converted/{Path(input_relpath).stem}.vtk"
    output_path = _prepare_run_output_path(run_id, output_relpath, overwrite=overwrite)
    fmt = _ovito_export_format(output_relpath, export_format)
    script = _write_helper_script(run_id, "ovito_convert.py", _OVITO_CONVERT_SCRIPT)
    result = _run_visual_command(
        [ovito_python, str(script), str(input_path), str(output_path), fmt],
        cwd=_run_dir(run_id),
        timeout=timeout,
    )
    return {
        "run_id": run_id,
        "tool": "ovito",
        "input_relpath": input_relpath,
        "output_relpath": str(output_path.relative_to(_run_dir(run_id))),
        "export_format": fmt,
        "output_exists": output_path.exists(),
        **result,
    }


_OVITO_RENDER_SCRIPT = r"""
from ovito.io import import_file
from ovito.vis import Viewport, TachyonRenderer
import sys

input_path, output_path, width, height, frame = sys.argv[1:6]
pipeline = import_file(input_path)
pipeline.add_to_scene()
vp = Viewport(type=Viewport.Type.Perspective)
vp.zoom_all()
vp.render_image(
    size=(int(width), int(height)),
    filename=output_path,
    frame=int(frame),
    renderer=TachyonRenderer(),
)
"""


@mcp.tool()
def render_with_ovito(
    run_id: str,
    input_relpath: str,
    output_relpath: str = "visualization/ovito_preview.png",
    width: int = 1280,
    height: int = 720,
    frame: int = 0,
    overwrite: bool = False,
    timeout: int | None = None,
) -> dict:
    """Render a PNG screenshot from a dump/trajectory using OVITO."""
    _ensure_write_allowed("render_with_ovito")
    if width < 1 or height < 1 or frame < 0:
        raise ValueError("width/height must be >= 1 and frame must be >= 0")
    ovito_python = _ovito_python()
    if not ovito_python:
        raise FileNotFoundError("OVITO Python module not found; set OVITO_PYTHON")
    input_path = _resolve_run_path(run_id, input_relpath)
    if not input_path.is_file():
        raise FileNotFoundError(input_relpath)
    output_path = _prepare_run_output_path(run_id, output_relpath, overwrite=overwrite)
    script = _write_helper_script(run_id, "ovito_render.py", _OVITO_RENDER_SCRIPT)
    result = _run_visual_command(
        [
            ovito_python,
            str(script),
            str(input_path),
            str(output_path),
            str(width),
            str(height),
            str(frame),
        ],
        cwd=_run_dir(run_id),
        timeout=timeout,
    )
    return {
        "run_id": run_id,
        "tool": "ovito",
        "input_relpath": input_relpath,
        "output_relpath": str(output_path.relative_to(_run_dir(run_id))),
        "output_exists": output_path.exists(),
        **result,
    }


_PARAVIEW_RENDER_SCRIPT = r"""
from paraview.simple import *
import sys
from pathlib import Path

input_path, output_path, width, height = sys.argv[1:5]
ext = Path(input_path).suffix.lower()
if ext == ".vtk":
    source = LegacyVTKReader(FileNames=[input_path])
elif ext == ".vtp":
    source = XMLPolyDataReader(FileName=[input_path])
elif ext == ".vtu":
    source = XMLUnstructuredGridReader(FileName=[input_path])
elif ext == ".csv":
    source = CSVReader(FileName=[input_path])
else:
    raise RuntimeError(f"Unsupported ParaView input extension: {ext}")
view = CreateView("RenderView")
view.ViewSize = [int(width), int(height)]
display = Show(source, view)
ResetCamera(view)
Render(view)
SaveScreenshot(output_path, view)
"""


@mcp.tool()
def render_with_paraview(
    run_id: str,
    input_relpath: str,
    output_relpath: str = "visualization/paraview_preview.png",
    width: int = 1280,
    height: int = 720,
    use_pvbatch: bool = False,
    overwrite: bool = False,
    timeout: int | None = None,
) -> dict:
    """Render a PNG screenshot from VTK/CSV data using ParaView."""
    _ensure_write_allowed("render_with_paraview")
    if width < 1 or height < 1:
        raise ValueError("width/height must be >= 1")
    pv_bin = _paraview_python(use_pvbatch)
    if not pv_bin:
        raise FileNotFoundError("no Python executable with paraview.simple was found")
    input_path = _resolve_run_path(run_id, input_relpath)
    if not input_path.is_file():
        raise FileNotFoundError(input_relpath)
    if input_path.suffix.lower() not in {".vtk", ".vtp", ".vtu", ".csv"}:
        raise ValueError("ParaView rendering supports .vtk, .vtp, .vtu, and .csv inputs")
    output_path = _prepare_run_output_path(run_id, output_relpath, overwrite=overwrite)
    script = _write_helper_script(run_id, "paraview_render.py", _PARAVIEW_RENDER_SCRIPT)
    result = _run_visual_command(
        [pv_bin, str(script), str(input_path), str(output_path), str(width), str(height)],
        cwd=_run_dir(run_id),
        timeout=timeout,
    )
    return {
        "run_id": run_id,
        "tool": "paraview",
        "input_relpath": input_relpath,
        "output_relpath": str(output_path.relative_to(_run_dir(run_id))),
        "output_exists": output_path.exists(),
        **result,
    }


@mcp.tool()
def summarize_visual_outputs(run_id: str) -> dict:
    """List images, animations, converted data, and visualization scripts."""
    records = _output_records(run_id, list(_VISUAL_OUTPUT_PATTERNS), include_bookkeeping=False)
    by_kind: dict[str, int] = {}
    total = 0
    for record in records:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
        total += record["size_bytes"]
    return {
        "run_id": run_id,
        "visual_output_count": len(records),
        "total_visual_bytes": total,
        "by_kind": by_kind,
        "outputs": records,
    }


@mcp.resource("liggghts://config", mime_type="application/json")
def config_resource() -> dict:
    """Current server configuration and safety limits."""
    return {
        "version": __version__,
        "liggghts_bin": LIGGGHTS_BIN,
        "liggghts_executable": _liggghts_executable(),
        "liggghts_resolved_path": str(_resolve_liggghts_executable())
        if _resolve_liggghts_executable()
        else None,
        "runs_root": str(RUNS),
        "read_only": _read_only(),
        "allow_overwrite": _env_bool("LIGGGHTS_ALLOW_OVERWRITE", False),
        "allow_deck_shell": _env_bool("LIGGGHTS_ALLOW_DECK_SHELL", False),
        "max_ranks": _env_int("LIGGGHTS_MAX_RANKS"),
        "max_concurrent_runs": _max_concurrent_runs(),
        "max_sweep_cases": _max_sweep_cases(),
        "max_read_bytes": _max_read_bytes(),
        "visualization_timeout": _vis_timeout(),
        "allowed_case_roots": [str(root) for root in _allowed_case_roots()],
    }


@mcp.resource("liggghts://runs", mime_type="application/json")
def runs_resource() -> list[dict]:
    """Known LIGGGHTS runs."""
    return list_runs()


@mcp.resource("liggghts://runs/{run_id}/status", mime_type="application/json")
def run_status_resource(run_id: str) -> dict:
    """Status for one LIGGGHTS run."""
    return check_status(run_id)


@mcp.resource("liggghts://runs/{run_id}/summary", mime_type="application/json")
def run_summary_resource(run_id: str) -> dict:
    """Compact status, log, and output summary for one run."""
    return summarize_run(run_id)


@mcp.resource("liggghts://runs/{run_id}/outputs", mime_type="application/json")
def run_outputs_resource(run_id: str) -> list[dict]:
    """Output files for one run."""
    return list_output_details(run_id)


@mcp.resource("liggghts://runs/{run_id}/input", mime_type="text/plain")
def run_input_resource(run_id: str) -> str:
    """Input deck for one run."""
    return _read_run_text(run_id, "input.in")


@mcp.resource("liggghts://runs/{run_id}/log", mime_type="text/plain")
def run_log_resource(run_id: str) -> str:
    """Tail of log.run for one run."""
    return read_log(run_id, tail=500)


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
    _ensure_write_allowed("stop_simulation")
    wd = _run_dir(run_id)
    pid = _read_pid(wd)
    if pid is None or not _alive(pid):
        return {"run_id": run_id, "status": "not_running"}
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return {"run_id": run_id, "status": "not_running"}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return {"run_id": run_id, "status": "not_running"}
    sig_used = "SIGTERM"
    # Best-effort wait; don't block MCP for long.
    _wait_until_group_not_alive(pgid, 2.0)
    if _process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
            sig_used = "SIGKILL"
            _wait_until_group_not_alive(pgid, 2.0)
        except ProcessLookupError:
            pass
    group_alive = _process_group_alive(pgid)
    status = _read_status(wd)
    if status:
        status = _finalize_if_done(run_id, wd, status)
        status["terminated_by_mcp"] = True
        status["termination_signal"] = sig_used
        default_exit_code = -signal.SIGKILL if sig_used == "SIGKILL" else -signal.SIGTERM
        if status.get("status") == "running" or not group_alive:
            status["status"] = "finished"
            status["finished_at"] = _now_iso()
        if status.get("exit_code") is None:
            status["exit_code"] = _read_exit_code_file(wd)
        if sig_used == "SIGKILL" and status.get("exit_code") in (None, -signal.SIGTERM):
            status["exit_code"] = default_exit_code
        if status.get("exit_code") is None:
            status["exit_code"] = default_exit_code
        _write_status(wd, status)
    _PROCS.pop(run_id, None)
    return {
        "run_id": run_id,
        "status": "terminated" if not group_alive else "termination_sent",
        "pid": pid,
        "pgid": pgid,
        "termination_signal": sig_used,
    }


@mcp.prompt()
def angle_of_repose_case(
    particle_count: int = 10000,
    particle_diameter_m: float = 0.002,
    material_density_kg_m3: float = 2500.0,
    run_steps: int = 50000,
) -> str:
    """Plan a generic LIGGGHTS angle-of-repose simulation."""
    return f"""Create a generic LIGGGHTS angle-of-repose DEM case.

Requirements:
- Use SI units and granular atom style.
- Target about {particle_count} spherical particles.
- Use particle diameter {particle_diameter_m} m and density {material_density_kg_m3} kg/m^3.
- Drop particles into a container or onto a flat floor, then let the pile settle.
- Run about {run_steps} steps unless the deck explains a better choice.
- Include dump output suitable for visualization.
- Avoid LIGGGHTS shell commands.

Workflow:
1. Draft the input deck.
2. Call estimate_case_cost, then validate_deck.
3. If validation is acceptable, start_simulation with a descriptive run name.
4. Poll check_status, inspect parse_log, and summarize_run when complete.
"""


@mcp.prompt()
def silo_discharge_case(
    particle_count: int = 20000,
    outlet_diameter_m: float = 0.02,
    run_steps: int = 100000,
) -> str:
    """Plan a generic silo discharge simulation."""
    return f"""Create a generic LIGGGHTS silo discharge DEM case.

Requirements:
- Use SI units and granular atom style.
- Model a vertical silo with an outlet diameter near {outlet_diameter_m} m.
- Use about {particle_count} particles unless a smaller smoke test is safer.
- Include a filling/settling stage and a discharge stage.
- Run about {run_steps} steps for the main discharge stage.
- Write dump output and a simple CSV or fix print signal for discharged mass/count if practical.
- Avoid LIGGGHTS shell commands.

Workflow:
1. Start with a small smoke-test deck.
2. Call validate_deck and estimate_case_cost.
3. Launch with start_simulation or start_from_dir.
4. Use parse_log, list_output_details, and summarize_run to evaluate the result.
"""


@mcp.prompt()
def mesh_wall_probe(
    mesh_file: str = "mesh.stl",
    run_steps: int = 0,
) -> str:
    """Plan a mesh wall capability and smoke-test case."""
    return f"""Create a minimal LIGGGHTS mesh wall probe.

Requirements:
- Use mesh file {mesh_file}.
- Use fix mesh/surface/stress and wall/gran mesh if the configured binary supports them.
- Seed one or a few particles so runtime construction is exercised.
- Run {run_steps} steps; use run 0 for a pure construction smoke test.
- Avoid LIGGGHTS shell commands.

Workflow:
1. Call validate_liggghts_bin with run_parse_probe=True and run_runtime_probe=True.
2. Draft a tiny deck using the mesh.
3. Call validate_deck.
4. Launch only after the binary capability probe passes.
"""


@mcp.prompt()
def parameter_sweep(
    parameter_name: str = "coefficientFriction",
    values: str = "0.2,0.4,0.6",
    base_run_name: str = "sweep",
) -> str:
    """Plan a small parameter sweep from a templated deck."""
    return f"""Create a small LIGGGHTS parameter sweep.

Parameter:
- Name: {parameter_name}
- Values: {values}

Workflow:
1. Draft one deck template that uses {{{{{parameter_name}}}}} where the value should vary.
2. Keep the first sweep small and cheap.
3. Call estimate_case_cost on one rendered case.
4. Call start_parameter_sweep with name_prefix="{base_run_name}".
5. Use compare_runs after completion to compare status, warnings, thermo data, and output sizes.
"""


if __name__ == "__main__":
    mcp.run()
