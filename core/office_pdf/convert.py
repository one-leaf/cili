"""COM worker：用本机 Office/WPS 把文档导出为 PDF。

由 ``cache._run_worker`` 以短命子进程调用；也支持
``python -m core.office_pdf.convert --worker ...``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_ENGINE_PROGIDS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "word": [
        ("Microsoft Office Word", ("Word.Application",)),
        ("WPS Writer", ("KWPS.Application", "WPS.Application")),
    ],
    "excel": [
        ("Microsoft Office Excel", ("Excel.Application",)),
        ("WPS Spreadsheets", ("KET.Application", "ET.Application")),
    ],
    "slides": [
        ("Microsoft Office PowerPoint", ("PowerPoint.Application",)),
        ("WPS Presentation", ("KWPP.Application", "WPP.Application")),
    ],
}

_WPS_EXE_NAMES = {"wps.exe", "kwps.exe", "et.exe", "ket.exe", "wpp.exe", "kwpp.exe"}


def _server_exe_name(command: str) -> str:
    """从 LocalServer32 命令行中取出可执行文件名（小写）。

    值形如 ``"C:\\...\\WINWORD.EXE" /Automation`` 或 ``C:\\...\\wps.exe``，
    直接取 basename 会把参数一起算进去。
    """
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        path = command[1:end] if end > 0 else command[1:]
    else:
        # 未加引号的值可能带空格（Office 就是 "C:\Program Files\...\EXCEL.EXE /automation"），
        # 按 .exe 边界切分，而不是按空格。
        end = command.lower().find(".exe")
        path = command[: end + 4] if end >= 0 else command.split(" ", 1)[0]
    return os.path.basename(path.replace("/", "\\")).lower()


# ProgID 的 CLSID 指向的本地服务程序 → 文档类型。用于识别注册表里
# 名字不熟悉的 Office/WPS 系 ProgID（只按名字猜容易误判）。
_SERVER_EXE_KINDS = {
    "wps.exe": "word", "kwps.exe": "word", "winword.exe": "word",
    "et.exe": "excel", "ket.exe": "excel", "excel.exe": "excel",
    "wpp.exe": "slides", "kwpp.exe": "slides", "powerpnt.exe": "slides",
}

_SERVER_EXE_LABELS = {
    "winword.exe": "Microsoft Office Word",
    "excel.exe": "Microsoft Office Excel",
    "powerpnt.exe": "Microsoft Office PowerPoint",
    "wps.exe": "WPS Writer", "kwps.exe": "WPS Writer",
    "et.exe": "WPS Spreadsheets", "ket.exe": "WPS Spreadsheets",
    "wpp.exe": "WPS Presentation", "kwpp.exe": "WPS Presentation",
}

def _write_office_pid(app: Any, pid_file: str) -> None:
    try:
        import ctypes

        hwnd = int(getattr(app, "Hwnd", 0) or 0)
        if not hwnd:
            return
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            Path(pid_file).write_text(str(pid.value), encoding="utf-8")
    except Exception:
        pass


def _configure_app(app: Any, kind: str) -> None:
    try:
        app.Visible = False
    except Exception:
        pass
    try:
        app.DisplayAlerts = False if kind == "excel" else 0
    except Exception:
        pass
    try:
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
    except Exception:
        pass


def _engine_candidates(kind: str) -> list[tuple[str, str]]:
    """静态引擎列表：[(引擎名, ProgID)]，按尝试顺序展开。"""
    return [
        (label, progid)
        for label, progids in _ENGINE_PROGIDS.get(kind, [])
        for progid in progids
    ]


def _probe_progid(progid: str) -> dict[str, dict[str, Any]]:
    """读取 ProgID 在 64/32 位注册表视图中的注册状态。

    返回 ``{"64": {...}, "32": {...}}``，每项字段：
    ``progid``（ProgID 键可见）、``clsid``、``clsid_visible``（CLSID 的
    LocalServer32 在同视图可见，即该视图的进程能激活它）、``server``。
    """
    status: dict[str, dict[str, Any]] = {}
    try:
        import winreg
    except ImportError:  # 非 Windows（测试环境）
        return status
    for view, const in (("64", "KEY_WOW64_64KEY"), ("32", "KEY_WOW64_32KEY")):
        flag = getattr(winreg, const, 0)
        entry: dict[str, Any] = {"progid": False, "clsid": "", "clsid_visible": False, "server": ""}
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID", 0,
                                winreg.KEY_READ | flag) as key:
                entry["progid"] = True
                entry["clsid"] = str(winreg.QueryValueEx(key, "")[0])
        except OSError:
            pass
        if entry["clsid"]:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT, rf"CLSID\{entry['clsid']}\LocalServer32",
                    0, winreg.KEY_READ | flag,
                ) as key:
                    entry["clsid_visible"] = True
                    entry["server"] = str(winreg.QueryValueEx(key, "")[0]).strip('"')
            except OSError:
                pass
        status[view] = entry
    return status


def _discover_office_engines() -> list[tuple[str, str, dict[str, dict[str, Any]]]]:
    """扫描注册表，找出已知列表之外的 Office/WPS ProgID。

    返回 ``[(kind, progid, 注册状态)]``。CLSID 指向 wps.exe / et.exe /
    wpp.exe / winword.exe 等服务程序的 ProgID 才会被收录。任何注册表访问
    失败都静默跳过——这只是失败后的补充手段，不能反过来影响正常路径。
    """
    try:
        import winreg
    except ImportError:
        return []

    names: dict[str, str] = {}
    for hive, root in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes"),
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
    ):
        for _view, const in (("64", "KEY_WOW64_64KEY"), ("32", "KEY_WOW64_32KEY")):
            flag = getattr(winreg, const, 0)
            try:
                with winreg.OpenKey(hive, root, 0, winreg.KEY_READ | flag) as key:
                    index = 0
                    while True:
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        index += 1
                        if name.lower().endswith(".application"):
                            names.setdefault(name.lower(), name)
            except OSError:
                continue

    known = {
        progid.lower()
        for entries in _ENGINE_PROGIDS.values()
        for _label, progids in entries
        for progid in progids
    }
    found: list[tuple[str, str, dict[str, dict[str, Any]]]] = []
    for name in names.values():
        if name.lower() in known:
            continue
        status = _probe_progid(name)
        kind = ""
        for view in ("64", "32"):
            server = status.get(view, {}).get("server", "")
            if server:
                kind = _SERVER_EXE_KINDS.get(_server_exe_name(server), "")
                if kind:
                    break
        if kind:
            found.append((kind, name, status))
    return found


def _discovered_candidates(kind: str) -> list[tuple[str, str]]:
    """失败兜底：扫描注册表，返回本进程视图可激活的同类 ProgID。"""
    labels = {"word": "WPS Writer", "excel": "WPS Spreadsheets", "slides": "WPS Presentation"}
    view = "64" if sys.maxsize > 2 ** 32 else "32"
    out = []
    for k, progid, status in _discover_office_engines():
        if k != kind:
            continue
        entry = status.get(view, {})
        if not entry.get("clsid_visible"):
            continue  # CLSID 只注册在另一视图：本进程无法激活
        exe = _server_exe_name(entry.get("server", ""))
        out.append((_SERVER_EXE_LABELS.get(exe, labels[kind]), progid))
    return out


def _format_registry_report() -> str:
    """生成 COM 注册状态摘要，附在转换失败详情后帮助定位环境问题。"""
    bits = "64" if sys.maxsize > 2 ** 32 else "32"
    checked = []
    only_other_view = []
    for kind in ("word", "excel", "slides"):
        for progid in (p for _l, ps in _ENGINE_PROGIDS.get(kind, []) for p in ps):
            status = _probe_progid(progid)
            v64 = "√" if status.get("64", {}).get("progid") else "×"
            v32 = "√" if status.get("32", {}).get("progid") else "×"
            checked.append(f"{progid}(64:{v64} 32:{v32})")
            if bits == "64" and status.get("32", {}).get("clsid_visible") \
                    and not status.get("64", {}).get("clsid_visible"):
                only_other_view.append(progid)

    wps_found = sorted({
        progid for _k, progid, status in _discover_office_engines()
        if any(
            _server_exe_name(status.get(view, {}).get("server", "")) in _WPS_EXE_NAMES
            for view in ("64", "32")
        )
    })
    lines = [f"COM 注册检查（Python {bits} 位）: " + " ".join(checked)]
    if only_other_view:
        lines.append(
            "仅注册在另一注册表视图、当前进程无法激活: " + ", ".join(only_other_view)
        )
    lines.append("扫描到的其它 WPS ProgID: " + (", ".join(wps_found) if wps_found else "无"))
    lines.append(
        "提示: 若已安装 WPS，请先手动打开一次 WPS，或运行「WPS Office 配置工具 → "
        "高级 → 重置修复 → 重新注册组件」后重试。"
    )
    return "\n".join(lines)


def _export_with_app(app: Any, kind: str, source: str, output: str) -> None:
    """用已创建的应用对象把 *source* 导出为 *output*（PDF）。"""
    if kind == "word":
        try:
            app.Options.UpdateLinksAtOpen = False
        except Exception:
            pass
        doc = app.Documents.Open(source, False, True, False)
        try:
            doc.SaveAs2(output, 17)  # wdFormatPDF
        except Exception:
            doc.SaveAs(output, 17)
        doc.Close(0)
    elif kind == "excel":
        try:
            app.AskToUpdateLinks = False
        except Exception:
            pass
        wb = app.Workbooks.Open(source, 0, True)
        wb.ExportAsFixedFormat(0, output)  # xlTypePDF
        wb.Close(False)
    else:
        pres = app.Presentations.Open(source, True, False, False)
        try:
            # ppSaveAsPDF=32。不要用 ExportAsFixedFormat：新版 PowerPoint
            # 类型库把最后一个参数 ExternalExporter 标成对象，pywin32 动态
            # 调用补默认值时抛 TypeError: The Python instance can not be
            # converted to a COM object（2026-11-12 自检复现）。
            pres.SaveAs(output, 32)
        except Exception as save_err:
            # 老版本 PowerPoint / 特殊文件回退到导出接口；两个错误都保留，
            # 否则只会看到回退接口的报错，反而掩盖真实原因。
            try:
                pres.ExportAsFixedFormat(output, 2)  # ppFixedFormatTypePDF
            except Exception as export_err:
                raise RuntimeError(
                    f"SaveAs failed: {save_err}; "
                    f"ExportAsFixedFormat failed: {export_err}"
                ) from export_err
        pres.Close()

    if not Path(output).exists() or Path(output).stat().st_size == 0:
        raise RuntimeError("conversion produced no PDF")


def _convert_with_com(source: str, output: str, kind: str, pid_file: str) -> str:
    import win32com.client as win32

    # PowerPoint 的 SaveAs 不接受正斜杠路径（报「无法将 … 保存到 …」），
    # 统一转成 Windows 原生分隔符；Word/Excel 同样兼容。
    source = os.path.normpath(source)
    output = os.path.normpath(output)

    errors: list[str] = []
    tried: set[str] = set()
    dispatched = False

    def attempt(label: str, progid: str) -> bool:
        nonlocal dispatched
        tried.add(progid.lower())
        app = None
        try:
            app = win32.DispatchEx(progid)
            dispatched = True
            _configure_app(app, kind)
            _write_office_pid(app, pid_file)
            _export_with_app(app, kind, source, output)
            return True
        except Exception as e:
            errors.append(f"{label} ({progid}): {e}")
            Path(output).unlink(missing_ok=True)
            return False
        finally:
            try:
                if app is not None:
                    app.Quit()
            except Exception:
                pass
            try:
                del app
            except Exception:
                pass

    for label, progid in _engine_candidates(kind):
        if attempt(label, progid):
            return label

    # 静态 ProgID 全部失败：扫描注册表里其它已注册的同类 ProgID
    # （WPS 各发行版命名不一致，例如只注册 wps.exe 对应的非标准 ProgID）。
    for label, progid in _discovered_candidates(kind):
        if progid.lower() in tried:
            continue
        if attempt(label, progid):
            return label

    detail = "; ".join(errors) or "no Office/WPS COM engine available"
    if not dispatched:
        # 一个引擎都没能创建：是 COM 注册/环境问题，附上注册表状态；
        # 反之（能创建但转换失败）多半是文件本身的问题，附注册表只会添噪音。
        detail += "\n" + _format_registry_report()
    raise RuntimeError(detail)


def _worker_main(argv: list[str]) -> int:
    import pythoncom

    if len(argv) != 4:
        print("usage: --worker <source> <output> <kind> <pid-file>", file=sys.stderr)
        return 2
    source, output, kind, pid_file = argv
    pythoncom.CoInitialize()
    try:
        engine = _convert_with_com(source, output, kind, pid_file)
        print(json.dumps({"engine": engine}, ensure_ascii=False))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2:]))
    raise SystemExit("Use the office2pdf tool from the agent, or --worker for internal use.")
