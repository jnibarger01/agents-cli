# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Background local server management for the ``run`` command."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import click
import psutil

from google.agents.cli._runner import popen_resolved_detached

_PID_DIR = ".google-agents-cli"
_PID_FILENAME = "run_server.json"
_LOG_FILENAME = "run_server.log"
_BASE_PORT = 18080
_MAX_PORT_ATTEMPTS = 10
_DEFAULT_IDLE_TIMEOUT = 1800  # 30 minutes
_LOGGER = logging.getLogger(__name__)


class ServerInfo(NamedTuple):
    """A running local server's port, and whether *this* call started it.

    ``started`` is ``True`` only when ``ensure_server`` launched a new
    process; it is ``False`` when an already-running server was reused.
    Callers use it to avoid tearing down a server someone else is keeping
    alive (e.g. one started with ``--start-server``).
    """

    port: int
    started: bool


def ensure_server(
    project_root: Path,
    agent_dir: str,
    *,
    idle_timeout: int = _DEFAULT_IDLE_TIMEOUT,
    trace_to_cloud: bool = False,
) -> ServerInfo:
    """Return a running local server's port, starting one if needed.

    If an existing server has been idle longer than *idle_timeout* seconds
    (based on the ``last_activity`` timestamp in the PID file), it is
    stopped and a fresh one is started.

    Args:
        project_root: The project root directory (cwd when running).
        agent_dir: The agent directory name (e.g. ``"investment_agent"``).
        idle_timeout: Seconds of inactivity before the server is considered
            stale and replaced.  Defaults to 30 minutes.
        trace_to_cloud: When ``True``, export traces to Cloud Trace.
            Only takes effect when a new server is started.

    Returns:
        A :class:`ServerInfo` with the port and whether this call started
        the server.
    """
    info = _read_pid_file(project_root)

    if info:
        if _is_server_alive(info["pid"], info["port"], info.get("create_time")):
            # Check idle timeout — stop the server if it's been idle too long.
            if _is_idle(info, idle_timeout):
                _cleanup(project_root, info)
            else:
                if trace_to_cloud and not info.get("trace_to_cloud"):
                    click.secho(
                        "Warning: reusing existing server that was started "
                        "without --trace-to-cloud.\n"
                        "  Run 'agents-cli run --stop-server' first to "
                        "restart with tracing enabled.",
                        fg="yellow",
                        err=True,
                    )
                _update_activity(project_root)
                return ServerInfo(info["port"], started=False)
        else:
            # Stale PID file — clean up before starting fresh.
            _cleanup(project_root, info)

    port = _find_free_port()
    pid = _start_server(project_root, agent_dir, port, trace_to_cloud=trace_to_cloud)
    create_time = _process_create_time(pid)
    try:
        _wait_for_port(port, pid=pid)
    except click.ClickException:
        # A failed readiness check must not leave an orphaned server behind.
        _cleanup(project_root, {"pid": pid, "create_time": create_time})
        raise
    _write_pid_file(
        project_root,
        pid=pid,
        port=port,
        create_time=create_time,
        trace_to_cloud=trace_to_cloud,
    )
    click.secho(f"Local server started on port {port} (PID {pid})", dim=True)
    click.secho("  Stop with: agents-cli run --stop-server", dim=True)
    return ServerInfo(port, started=True)


def stop_server(project_root: Path) -> bool:
    """Stop the background server.

    Returns:
        ``True`` if a server was found and stopped.
    """
    info = _read_pid_file(project_root)
    if not info:
        return False
    _cleanup(project_root, info)
    click.secho("Local server stopped.", dim=True)
    return True


def get_server_port(project_root: Path) -> int | None:
    """Return the port of the running local server, or ``None`` if absent.

    Returns ``None`` when no server has been started for *project_root*,
    or when the recorded process is no longer alive.
    """
    info = _read_pid_file(project_root)
    if not info:
        return None
    if not _is_server_alive(info["pid"], info["port"], info.get("create_time")):
        return None
    return info["port"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_free_port(
    base: int = _BASE_PORT, max_attempts: int = _MAX_PORT_ATTEMPTS
) -> int:
    """Find a free local port starting from *base*."""
    for offset in range(max_attempts):
        port = base + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise click.ClickException(
        f"No free port found in range {base}–{base + max_attempts - 1}.\n"
        "  Stop other servers or use --url to query a remote agent."
    )


def _start_server(
    project_root: Path,
    agent_dir: str,
    port: int,
    *,
    trace_to_cloud: bool = False,
) -> int:
    """Start ``adk api_server`` as a detached background process.

    Returns the PID.
    """
    adk_dir = project_root / _PID_DIR
    adk_dir.mkdir(exist_ok=True)
    log_path = adk_dir / _LOG_FILENAME

    cmd = [
        "uv",
        "run",
        "adk",
        "api_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--reload_agents",
    ]
    if trace_to_cloud:
        cmd.append("--trace_to_cloud")
    cmd.append(".")

    # Use in-memory sessions locally so the server can start without
    # cloud dependencies (e.g. Agent Runtime session type).
    env = os.environ.copy()
    env.setdefault("USE_IN_MEMORY_SESSION", "true")

    with open(log_path, "a", encoding="utf-8") as log_file:
        proc = popen_resolved_detached(
            cmd,
            cwd=str(project_root),
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
    return proc.pid


def _wait_for_port(port: int, timeout: int = 30, pid: int | None = None) -> None:
    """Wait until a local server is ready to handle HTTP requests.

    Polls with an HTTP GET so the server's lifespan (which registers
    routes like A2A endpoints) has time to complete.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Fail fast if the server process has already crashed.
        if pid is not None:
            try:
                os.kill(pid, 0)
            except OSError:
                raise click.ClickException(
                    "Local server process exited during startup.\n"
                    f"  Check logs: {_PID_DIR}/{_LOG_FILENAME}"
                ) from None
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except urllib.error.HTTPError:
            # Any HTTP response (even 404/405) means the server is ready.
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    raise click.ClickException(
        f"Local server did not start within {timeout}s.\n"
        f"  Check logs: {_PID_DIR}/{_LOG_FILENAME}"
    )


# --- PID file helpers ---


def _pid_file_path(project_root: Path) -> Path:
    return project_root / _PID_DIR / _PID_FILENAME


def _read_pid_file(project_root: Path) -> dict | None:
    path = _pid_file_path(project_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    port = data.get("port")
    create_time = data.get("create_time")
    if (
        type(pid) is not int
        or pid <= 0
        or type(port) is not int
        or not 1 <= port <= 65535
        or (
            create_time is not None
            and not isinstance(create_time, (int, float))
        )
    ):
        return None
    return data


def _write_pid_file(
    project_root: Path,
    *,
    pid: int,
    port: int,
    create_time: float | None,
    trace_to_cloud: bool = False,
) -> None:
    now = datetime.now(UTC).isoformat()
    data = {
        "pid": pid,
        "port": port,
        "create_time": create_time,
        "started_at": now,
        "last_activity": now,
        "trace_to_cloud": trace_to_cloud,
    }
    path = _pid_file_path(project_root)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _update_activity(project_root: Path) -> None:
    """Stamp ``last_activity`` so idle detection resets."""
    path = _pid_file_path(project_root)
    try:
        data = json.loads(path.read_text())
        data["last_activity"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError):
        pass


def _is_idle(info: dict, idle_timeout: int) -> bool:
    """Return ``True`` if the server has been idle longer than *idle_timeout*."""
    try:
        last = datetime.fromisoformat(info["last_activity"])
        idle = (datetime.now(UTC) - last).total_seconds()
        return idle > idle_timeout
    except (KeyError, ValueError):
        # Treat missing or unparseable timestamps as stale.
        return True


def _is_server_alive(pid: int, port: int, create_time: float | None = None) -> bool:
    """Return whether the recorded process owns the port.

    The creation time prevents a recycled PID from being mistaken for the
    local server. In particular, callers must not terminate a process based
    only on a stale PID file.
    """
    process = _owned_process(pid, create_time)
    if process is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _process_create_time(pid: int) -> float | None:
    """Return a process creation timestamp, or ``None`` if it exited."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
        return None


def _owned_process(pid: int, create_time: float | None) -> psutil.Process | None:
    """Return the process only when the PID file still identifies it."""
    if create_time is None:
        return None
    try:
        process = psutil.Process(pid)
        if process.create_time() != create_time:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
        return None


def _cleanup(project_root: Path, info: dict) -> None:
    """Terminate the server process and remove the PID file."""
    pid = info.get("pid")
    process = _owned_process(pid, info.get("create_time")) if pid else None
    if process:
        try:
            children = process.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            process.terminate()
            psutil.wait_procs([*children, process], timeout=3)
        except psutil.NoSuchProcess:
            pass
    elif pid:
        _LOGGER.warning(
            "Skipping termination of PID %d because its process identity could not "
            "be verified.",
            pid,
        )
    path = _pid_file_path(project_root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning("Failed to remove PID file %s: %s", path, exc)
