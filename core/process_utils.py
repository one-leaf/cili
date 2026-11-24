"""进程/端口探测：监听 PID、进程命令行（Windows CIM/netstat、POSIX lsof/ps）。

``port_guard`` 与浏览器服务的进程管理共用同一套探测实现，避免两套口径。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_TCP_STATES = {"TCP", "TCPV6"}


def _local_port(address: str) -> int | None:
    """Extract the port from a netstat local-address column (``[::1]:8000``)."""
    try:
        return int(address.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _cim_listener_pids(port: int) -> list[int] | None:
    """Listener PIDs from the Windows connection table (CIM).

    ``netstat`` on some Windows builds omits newly created LISTENING rows, so
    the CIM table is the primary source. A listener row has RemotePort 0;
    established connections on the same local port carry a nonzero RemotePort.

    Returns None when the query itself failed, so the caller may fall back to
    ``netstat``. An empty list means the table was read and no listener row
    matched the port; falling back to netstat then could resurrect stale rows.
    """
    if sys.platform != "win32":
        return None
    script = (
        f"Get-NetTCPConnection -LocalPort {int(port)} -ErrorAction SilentlyContinue | "
        "Select-Object RemotePort,OwningProcess | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        logger.debug("[setup] CIM port scan failed for %s: %s", port, e)
        return None
    text = result.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("[setup] CIM port scan returned invalid JSON for %s", port)
        return None
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    pids: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("RemotePort") not in (0, "0", None, ""):
            continue
        try:
            pid = int(row.get("OwningProcess") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return list(dict.fromkeys(pids))


def _netstat_listener_pids(port: int) -> list[int]:
    """Listener PIDs parsed from ``netstat``/``lsof`` (fallback source)."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10,
            )
            pids: list[int] = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() not in _TCP_STATES:
                    continue
                try:
                    state_idx = parts.index("LISTENING")
                except ValueError:
                    continue
                if state_idx + 1 >= len(parts):
                    continue
                if _local_port(parts[1]) != int(port):
                    continue
                pid = parts[state_idx + 1]
                if pid.isdigit() and int(pid) > 0:
                    pids.append(int(pid))
            # Multiple rows can report the same listener (IPv4 + IPv6).
            return list(dict.fromkeys(pids))

        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-s", "tcp:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        return [
            int(token) for token in result.stdout.split()
            if token.isdigit() and int(token) > 0
        ]
    except Exception as e:
        logger.debug("[setup] Port scan failed for %s: %s", port, e)
        return []


def find_listener_pids(port: int) -> list[int]:
    """Return PIDs listening on ``port`` (CIM first on Windows)."""
    cim_pids = _cim_listener_pids(port)
    if cim_pids is not None:
        return cim_pids
    return _netstat_listener_pids(port)


def get_process_command_line(pid: int) -> str | None:
    """Return the full command line of ``pid``, or None when unavailable."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=10,
            )
            text = result.stdout.strip()
            return text or None
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=10,
        )
        text = result.stdout.strip()
        return text or None
    except Exception as e:
        logger.debug("[setup] Failed to read command line for PID %s: %s", pid, e)
        return None
