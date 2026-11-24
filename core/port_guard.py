"""Web server listen-port probing and conflict resolution.

Startup policy (see ``resolve_web_port``):

- A free port is used as-is.
- If the port is held by Cili itself, the old instance is terminated so the
  same port can be reused.
- If the port is held by a foreign program, the next free port is used, so a
  port conflict never blocks startup.

Cili detection is deliberately conservative: listener PIDs come from the
Windows CIM connection table (``netstat`` as fallback), then a process is
accepted as Cili only when one of these holds:

- ``data/cili/server.json`` records the same PID and port (and the process
  command line is unavailable, or is a Python process running ``main.py``);
- the command line runs ``main.py`` from a Cili directory;
- the port answers ``/openapi.json`` with the ``Cili Web API`` title.

Anything else is treated as foreign and is never killed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from core.process_utils import (  # noqa: E402 —— 探测实现集中在该模块
    find_listener_pids,
    get_process_command_line,
)

# core/port_guard.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_INFO_PATH = PROJECT_ROOT / "data" / "cili" / "server.json"

# Port scan range after the requested port, and timing for releasing a killed
# previous instance.
DEFAULT_PORT_SCAN_LIMIT = 50
KILL_RELEASE_TIMEOUT = 8.0
KILL_POLL_INTERVAL = 0.2
HTTP_PROBE_TIMEOUT = 1.0

def is_port_free(host: str, port: int) -> bool:
    """Return True when a TCP listener can bind ``host:port`` right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR on purpose: on Windows it would allow binding an
        # address that another process is already listening on, hiding conflicts.
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _is_cili_command_line(cmdline: str) -> bool:
    """True when a command line runs a Python ``main.py`` from a Cili directory."""
    normalized = cmdline.replace("\\", "/").lower()
    if "main.py" not in normalized or "python" not in normalized:
        return False
    root = str(PROJECT_ROOT).replace("\\", "/").lower()
    return root in normalized or "/cili/" in normalized


def _read_server_info() -> dict:
    """Load ``data/cili/server.json``; empty dict when missing or invalid."""
    try:
        with open(SERVER_INFO_PATH, encoding="utf-8") as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _probe_host(host: str) -> str:
    """Address to probe: wildcard binds are reachable through loopback."""
    if host in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "::1"
    return host


def probe_cili_http(host: str, port: int, token: str = "") -> bool:
    """Fingerprint a running Cili server through its FastAPI OpenAPI title."""
    probe_host = _probe_host(host)
    url_host = f"[{probe_host}]" if ":" in probe_host else probe_host
    url = f"http://{url_host}:{port}/openapi.json"
    headers = {"X-Access-Token": token} if token else {}
    try:
        import httpx

        # trust_env=False: local probes must not be routed through a proxy
        # configured for LLM endpoints in the environment.
        with httpx.Client(trust_env=False, timeout=HTTP_PROBE_TIMEOUT) as client:
            response = client.get(url, headers=headers)
        return "Cili Web API" in response.text
    except Exception:
        return False


def _server_info_matches(pid: int, port: int) -> bool:
    info = _read_server_info()
    try:
        return (
            int(info.get("pid") or 0) == int(pid)
            and int(info.get("port") or 0) == int(port)
        )
    except (TypeError, ValueError):
        return False


def _server_info_fallback_pids(port: int) -> list[int]:
    """Last resort when the OS table is unreadable: trust server.json + cmdline."""
    info = _read_server_info()
    try:
        pid = int(info.get("pid") or 0)
        recorded = int(info.get("port") or 0)
    except (TypeError, ValueError):
        return []
    if pid <= 0 or pid == os.getpid() or recorded != int(port):
        return []
    # Require a Cili command line here: server.json alone may be stale and its
    # PID reused by an unrelated process, which must never be killed.
    cmdline = get_process_command_line(pid)
    if cmdline and _is_cili_command_line(cmdline):
        return [pid]
    return []


def is_cili_listener(pid: int, port: int, host: str, probe_token: str = "") -> bool:
    """Decide whether the listener ``pid`` on ``port`` is a Cili instance."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False

    info_match = _server_info_matches(pid, port)
    cmdline = get_process_command_line(pid)
    normalized = (cmdline or "").replace("\\", "/").lower()
    runs_main_py = "main.py" in normalized and "python" in normalized

    if _is_cili_command_line(cmdline or ""):
        return True
    # server.json + Python/main.py corroboration covers relative launches such
    # as ``python main.py`` from the project root, which carry no Cili path.
    if info_match and (not cmdline or runs_main_py):
        return True
    return probe_cili_http(host, port, probe_token)


def kill_process(pid: int) -> bool:
    """Terminate a single PID (not its children), returning success."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if sys.platform == "win32":
            # /F only: /T would also kill Chrome, which BrowserService
            # intentionally keeps alive across Cili restarts.
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception as e:
        logger.warning("[setup] Failed to stop PID %s: %s", pid, e)
        return False


def wait_port_free(host: str, port: int, timeout: float = KILL_RELEASE_TIMEOUT) -> bool:
    """Poll until ``host:port`` can be bound or ``timeout`` expires."""
    deadline = time.monotonic() + timeout
    while True:
        if is_port_free(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(KILL_POLL_INTERVAL)


def _pick_os_port(host: str) -> int:
    """Ask the OS for an unused TCP port on ``host``."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    except OSError as e:
        raise RuntimeError(f"Failed to find a free port for host {host}: {e}") from e
    finally:
        sock.close()


def _find_next_free_port(host: str, start: int, max_scan: int) -> int:
    for offset in range(1, max(1, int(max_scan)) + 1):
        candidate = int(start) + offset
        if candidate > 65535:
            break
        if is_port_free(host, candidate):
            return candidate
    return _pick_os_port(host)


def resolve_web_port(
    host: str,
    desired_port: int,
    *,
    probe_token: str = "",
    max_scan: int = DEFAULT_PORT_SCAN_LIMIT,
) -> int:
    """Return a usable web port, resolving conflicts per the startup policy."""
    desired = int(desired_port)
    if desired <= 0 or desired > 65535:
        return _pick_os_port(host)

    if is_port_free(host, desired):
        return desired

    listener_pids = find_listener_pids(desired)
    fallback_pids = [] if listener_pids else _server_info_fallback_pids(desired)
    cili_pids = [
        pid for pid in listener_pids
        if is_cili_listener(pid, desired, host, probe_token)
    ]
    cili_pids.extend(fallback_pids)
    if cili_pids:
        for pid in cili_pids:
            logger.info(
                "[setup] Stopping previous Cili instance (PID %s) on port %s",
                pid, desired,
            )
            if kill_process(pid) and wait_port_free(host, desired):
                logger.info(
                    "[setup] Port %s released by the previous Cili instance", desired,
                )
                return desired
            logger.warning(
                "[setup] Port %s is still busy after stopping PID %s", desired, pid,
            )
            break
    elif listener_pids:
        logger.warning(
            "[setup] Port %s is held by a non-Cili process (PID %s)",
            desired, ", ".join(str(pid) for pid in listener_pids),
        )

    replacement = _find_next_free_port(host, desired, max_scan)
    logger.warning("[setup] Using port %s instead of %s", replacement, desired)
    return replacement
