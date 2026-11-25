"""Windows 目录占用诊断：找出可能阻止目录改名/删除的进程。

目录移动失败有两条典型原因，对应不同的错误码，需要分别检测：

- 目录内（或目录自身）有文件被进程打开 → WinError 5（拒绝访问）
  做法：反向枚举系统句柄表，挑出落在目标路径下的文件句柄，反查持有进程。
  逐文件询问 Restart Manager 是 10ms/文件 量级，上万文件的目录要几分钟；
  反向枚举一次只花 1–2 秒，与目录大小无关。
- 某进程的当前工作目录在该目录内 → WinError 32（共享冲突）
  做法：读取每个进程 PEB 里的 CurrentDirectory。

仅供失败后的诊断使用；非 Windows 或任何一步出错都返回已发现的部分。
"""

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes as wt
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_PROCESS_DUP_HANDLE = 0x0040
_DUPLICATE_SAME_ACCESS = 0x2

_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_TYPE_DISK = 1
_INVALID_HANDLE = ctypes.c_void_p(-1).value

_MAX_LINES = 8


if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.GetCurrentProcess.restype = wt.HANDLE
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE,
                                          ctypes.POINTER(wt.HANDLE), wt.DWORD,
                                          wt.BOOL, wt.DWORD]
    _kernel32.CreateFileW.restype = wt.HANDLE
    _kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                      wt.DWORD, wt.DWORD, wt.HANDLE]
    _kernel32.GetFileType.restype = wt.DWORD
    _kernel32.GetFileType.argtypes = [wt.HANDLE]
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    _kernel32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                     ctypes.POINTER(wt.DWORD)]
    _kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_size_t)]
    _ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                                wt.ULONG, ctypes.POINTER(wt.ULONG)]
    _ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p,
                                                 wt.ULONG, ctypes.POINTER(wt.ULONG)]


def describe_holders(path: str | os.PathLike) -> list[str]:
    """返回占用 ``path`` 的进程说明列表（空列表表示没找到或非 Windows）。"""
    if not _IS_WINDOWS:
        return []
    lines = _file_holder_lines(Path(path)) + _cwd_holder_lines(Path(path))
    return lines


# ── 文件句柄（WinError 5）──────────────────────────────────────

def _file_holder_lines(root: Path) -> list[str]:
    try:
        hits = _scan_file_handles(root)
    except Exception:  # noqa: BLE001 —— 诊断失败不影响主流程
        return []
    lines = []
    for pid in sorted(hits):
        paths = sorted(set(hits[pid]))
        detail = paths[0] if len(paths) == 1 else f"{paths[0]} 等 {len(paths)} 个文件"
        lines.append(f"{_process_name(pid)}（PID {pid}）打开了 {detail}")
    return _cap(lines)


def _scan_file_handles(root: Path) -> dict[int, list[str]]:
    """枚举系统句柄表，返回打开着 ``root`` 下文件的 {pid: [路径]}。"""
    root_nc = os.path.normcase(os.path.abspath(str(root)))
    prefix = root_nc + os.sep

    fd, probe_path = tempfile.mkstemp()
    os.close(fd)
    probe = _kernel32.CreateFileW(probe_path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL,
                                  None, _OPEN_EXISTING, 0, None)
    try:
        if not probe or probe == _INVALID_HANDLE:
            return {}
        entries = _enum_handles()
        me = os.getpid()
        file_type = next((t for pid, handle, t in entries
                          if pid == me and handle == probe), None)
        if file_type is None:
            return {}

        hits: dict[int, list[str]] = {}
        opened: dict[int, int | None] = {}
        current = _kernel32.GetCurrentProcess()
        try:
            for pid, handle, type_index in entries:
                if type_index != file_type:
                    continue
                if pid not in opened:
                    opened[pid] = _kernel32.OpenProcess(
                        _PROCESS_DUP_HANDLE | _PROCESS_QUERY_LIMITED_INFORMATION,
                        False, pid)
                proc = opened[pid]
                if not proc:
                    continue
                dup = wt.HANDLE()
                if not _kernel32.DuplicateHandle(proc, handle, current,
                                                 ctypes.byref(dup), 0, False,
                                                 _DUPLICATE_SAME_ACCESS):
                    continue
                try:
                    found = _final_path(dup)
                finally:
                    _kernel32.CloseHandle(dup)
                if not found:
                    continue
                found_nc = os.path.normcase(found)
                if found_nc == root_nc or found_nc.startswith(prefix):
                    hits.setdefault(pid, []).append(found)
        finally:
            for proc in opened.values():
                if proc:
                    _kernel32.CloseHandle(proc)
        return hits
    finally:
        if probe and probe != _INVALID_HANDLE:
            _kernel32.CloseHandle(probe)
        try:
            os.unlink(probe_path)
        except OSError:
            pass


def _enum_handles() -> list[tuple[int, int, int]]:
    """一次取回全部 (pid, handle, object_type_index)；句柄表大时按需扩容。"""
    size = 1 << 22
    while True:
        buf = ctypes.create_string_buffer(size)
        used = wt.ULONG()
        status = _ntdll.NtQuerySystemInformation(
            _SYSTEM_EXTENDED_HANDLE_INFORMATION, buf, size, ctypes.byref(used),
        ) & 0xFFFFFFFF
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, used.value + 0x10000)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation 失败: {status:#x}")
        break

    raw = buf.raw[: used.value]
    ptr = ctypes.sizeof(ctypes.c_void_p)
    entry_size = 3 * ptr + 16  # Object + pid + handle + access/type/attr 字段
    count = int.from_bytes(raw[:ptr], "little")
    entries = []
    for i in range(count):
        base = 2 * ptr + i * entry_size
        chunk = raw[base: base + entry_size]
        if len(chunk) < entry_size:
            break
        pid = int.from_bytes(chunk[ptr: 2 * ptr], "little")
        handle = int.from_bytes(chunk[2 * ptr: 3 * ptr], "little")
        type_index = int.from_bytes(chunk[3 * ptr + 6: 3 * ptr + 8], "little")
        entries.append((pid, handle, type_index))
    return entries


def _final_path(handle) -> str | None:
    """句柄对应的 DOS 路径；非磁盘对象（管道等）跳过——查名可能阻塞。"""
    if _kernel32.GetFileType(handle) != _FILE_TYPE_DISK:
        return None
    length = 1024
    while length <= 32768:
        buf = ctypes.create_unicode_buffer(length)
        needed = _kernel32.GetFinalPathNameByHandleW(handle, buf, length, 0)
        if not needed:
            return None
        if needed < length:
            value = buf.value
            return value[4:] if value.startswith("\\\\?\\") else value
        length = needed + 1
    return None


# ── 工作目录（WinError 32）────────────────────────────────────

def _cwd_holder_lines(root: Path) -> list[str]:
    try:
        pids = _enum_pids()
    except Exception:  # noqa: BLE001
        return []
    root_nc = os.path.normcase(os.path.abspath(str(root)))
    prefix = root_nc + os.sep
    lines = []
    for pid in pids:
        cwd, exe = _process_cwd_and_exe(pid)
        cwd_nc = os.path.normcase((cwd or "").rstrip("\\/"))
        exe_nc = os.path.normcase(exe or "")
        if cwd_nc and (cwd_nc == root_nc or cwd_nc.startswith(prefix)):
            lines.append(f"{_process_name(pid)}（PID {pid}）的工作目录在此目录内")
        elif exe_nc and (exe_nc == root_nc or exe_nc.startswith(prefix)):
            lines.append(f"{_process_name(pid)}（PID {pid}）的可执行文件在此目录内")
    return _cap(lines)


def _enum_pids() -> list[int]:
    psapi = ctypes.WinDLL("psapi")
    arr = (wt.DWORD * 8192)()
    used = wt.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(used)):
        return []
    return [arr[i] for i in range(used.value // 4)]


def _process_cwd_and_exe(pid: int) -> tuple[str | None, str | None]:
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None, None
    try:
        return _read_cwd(handle), _image_path(handle)
    finally:
        _kernel32.CloseHandle(handle)


def _image_path(handle) -> str | None:
    length = wt.DWORD(1024)
    buf = ctypes.create_unicode_buffer(1024)
    if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
        return buf.value
    return None


def _process_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "?"
    try:
        exe = _image_path(handle)
    finally:
        _kernel32.CloseHandle(handle)
    return os.path.basename(exe) if exe else "?"


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def _read_cwd(handle) -> str | None:
    """读目标进程 PEB → RTL_USER_PROCESS_PARAMETERS.CurrentDirectory。"""
    try:
        info = _PROCESS_BASIC_INFORMATION()
        ret = wt.ULONG()
        status = _ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(ret),
        ) & 0xFFFFFFFF
        if status != 0 or not info.PebBaseAddress:
            return None
        ptr = ctypes.sizeof(ctypes.c_void_p)
        if _is_wow64(handle):  # 32 位进程的 PEB 布局不同
            params_offset, curdir_offset = 0x10, 0x24
        else:
            params_offset, curdir_offset = 0x20, 0x38
        params = int.from_bytes(
            _read_memory(handle, info.PebBaseAddress + params_offset, ptr), "little")
        if not params:
            return None
        curdir = _read_memory(handle, params + curdir_offset, 2 * ptr)
        length = int.from_bytes(curdir[0:2], "little")
        buffer_ptr = int.from_bytes(curdir[ptr:2 * ptr], "little")
        if not length or not buffer_ptr:
            return None
        return _read_memory(handle, buffer_ptr, length).decode("utf-16-le", "replace")
    except OSError:
        return None


def _is_wow64(handle) -> bool:
    flag = wt.BOOL()
    return bool(_kernel32.IsWow64Process(handle, ctypes.byref(flag))) and bool(flag.value)


def _read_memory(handle, address: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not _kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size,
                                       ctypes.byref(got)):
        raise OSError(ctypes.get_last_error(), "ReadProcessMemory")
    return buf.raw[: got.value]


def _cap(lines: list[str]) -> list[str]:
    if len(lines) > _MAX_LINES:
        return lines[:_MAX_LINES] + [f"…等 {len(lines)} 个进程"]
    return lines