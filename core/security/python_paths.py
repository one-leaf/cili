"""python 代码写/删目标收集：AST 静态分析路径权限门。

Pass1 收集别名（os/shutil/pathlib、from pathlib import Path as P）；
Pass2 简单符号表（字面量赋值 → 可解析路径，非字面量 → 动态）；
Pass3 扫函数调用，把写/删操作的目标收成 PathTarget 列表：

- os.remove/unlink/rmdir/removedirs → delete
- os.rename/renames/replace → delete(src) + write(dst)
- os.mkdir/makedirs → write
- shutil.rmtree → delete；shutil.move → delete+write；shutil.copy/copy2/copyfile/copytree → write(末参)
- Path(...).unlink/rmdir → delete；.rename/replace → delete+write；.mkdir/.touch/.write_text/.write_bytes → write
- Path(...).open / open(path, mode)：mode 含 w/a/x/+ → write，缺省或 r → 跳过（读），mode 动态 → 保守 write

非字面量参数（变量/调用/动态构造）→ dynamic=True（resolved=None），保守按越界待审。
已知局限：库方法内部（df.to_csv 等）AST 不可见，本轮只覆盖直接调用。
"""

from __future__ import annotations

import ast
import os

from core.security.path_policy import OP_DELETE, OP_WRITE, PathTarget

_DYN = object()   # 动态/不可静态解析
_READ = object()  # 缺省读模式

_OS_DELETE_ATTRS = {"remove", "unlink", "rmdir", "removedirs"}
_OS_MKDIR_ATTRS = {"mkdir", "makedirs"}
_OS_RENAME_ATTRS = {"rename", "renames", "replace"}  # delete+write
_SHUTIL_DELETE_ATTRS = {"rmtree"}
_SHUTIL_MOVE_ATTRS = {"move"}
_SHUTIL_WRITE_ATTRS = {"copy", "copy2", "copyfile", "copytree"}
_PATH_DELETE_ATTRS = {"unlink", "rmdir"}
_PATH_RENAME_ATTRS = {"rename", "replace"}
_PATH_WRITE_ATTRS = {"mkdir", "touch", "write_text", "write_bytes"}
_OPEN_WRITE_CHARS = "wax+"


def _collect_aliases(tree):
    """收集 os/shutil/pathlib 模块别名与 Path 直接导入名。"""
    os_names = {"os"}
    shutil_names = {"shutil"}
    path_names = {"Path"}
    pathlib_mods = {"pathlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_names.add(alias.asname or "os")
                elif alias.name == "shutil":
                    shutil_names.add(alias.asname or "shutil")
                elif alias.name == "pathlib":
                    pathlib_mods.add(alias.asname or "pathlib")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pathlib":
                for alias in node.names:
                    if alias.name == "Path":
                        path_names.add(alias.asname or "Path")
            elif node.module == "os":
                for alias in node.names:
                    if alias.name == "path":
                        os_names.add(alias.asname or "path")
    return os_names, shutil_names, path_names, pathlib_mods


def _resolve_py_path(value: str, cwd: str) -> str:
    p = os.path.expanduser(value)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


class _Collector:
    def __init__(self, code: str, cwd: str, os_names, shutil_names, path_names, pathlib_mods):
        self.code = code
        self.cwd = cwd
        self.os_names = os_names
        self.shutil_names = shutil_names
        self.path_names = path_names
        self.pathlib_mods = pathlib_mods
        self.targets: list[PathTarget] = []
        self.symtab: dict[str, object] = {}
        self.path_vars: set[str] = set()

    # ── 表达式解析 ──────────────────────────────────────────────

    def resolve(self, node: ast.AST):
        """表达式 → 字面量路径 str | _DYN。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return str(node.value)
        if isinstance(node, ast.Name):
            return self.symtab.get(node.id, _DYN)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self.resolve(node.left)
            right = self.resolve(node.right)
            if left is _DYN or right is _DYN:
                return _DYN
            return os.path.join(left, right)
        if isinstance(node, ast.Call):
            joined = self._join_args(node)
            if joined is not None:
                return joined
        return _DYN

    def _join_args(self, node: ast.Call):
        """Path(a, b) / pl.Path(a) / os.path.join(a, b) 的多参拼接。"""
        func = node.func
        parts = None
        if isinstance(func, ast.Attribute) and func.attr == "join":
            v = func.value
            if isinstance(v, ast.Attribute) and v.attr == "path" \
                    and isinstance(v.value, ast.Name) and v.value.id in self.os_names:
                parts = node.args
            elif isinstance(v, ast.Name) and v.id in self.os_names:
                parts = node.args
        elif isinstance(func, ast.Name) and func.id in self.path_names:
            parts = node.args
        elif isinstance(func, ast.Attribute) and func.attr == "Path" \
                and isinstance(func.value, ast.Name) and func.value.id in self.pathlib_mods:
            parts = node.args
        if parts is None:
            return None
        pieces = []
        for a in parts:
            v = self.resolve(a)
            if v is _DYN:
                return _DYN
            pieces.append(v)
        return os.path.join(*pieces)

    def is_path_expr(self, node: ast.AST) -> bool:
        """是否为 Path 对象构造/拼接表达式（用于识别 path_vars 与实例方法）。"""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return True
        if isinstance(node, ast.Call):
            if self._join_args(node) is not None:
                return True
        return False

    # ── Pass 2：符号表 ──────────────────────────────────────────

    def collect_symbols(self, tree) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if node.value is None:
                    continue
                resolved = self.resolve(node.value)
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self.symtab[tgt.id] = resolved
                        if self.is_path_expr(node.value):
                            self.path_vars.add(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    resolved = self.resolve(node.value)
                    self.symtab[node.target.id] = resolved
                    if self.is_path_expr(node.value):
                        self.path_vars.add(node.target.id)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                self.symtab[node.target.id] = _DYN  # 拼接改写后不可静态解析

    # ── Pass 3：调用扫描 ────────────────────────────────────────

    def emit(self, op: str, op_name: str, node: ast.AST, value: object) -> None:
        if value is _DYN:
            raw = ast.get_source_segment(self.code, node) or ast.unparse(node)
            self.targets.append(PathTarget(op, raw, op_name=op_name, dynamic=True))
        else:
            self.targets.append(PathTarget(
                op, value, op_name=op_name, resolved=_resolve_py_path(value, self.cwd),
            ))

    def _mode(self, node: ast.Call):
        """open/Path.open 的 mode 参数 → 字面量 str | _READ | _DYN。"""
        for kw in node.keywords:
            if kw.arg == "mode":
                v = kw.value
                return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _DYN
        if len(node.args) >= 2:
            v = node.args[1]
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _DYN
        return _READ

    def _is_write_mode(self, mode: object) -> bool:
        if mode is _READ:
            return False
        if mode is _DYN:
            return True
        return any(ch in mode for ch in _OPEN_WRITE_CHARS)

    def _base_path(self, node: ast.AST):
        """Path 构造/拼接表达式 → 解析值（str | _DYN）；非 Path 表达式 → None。"""
        if not self.is_path_expr(node):
            return None
        return self.resolve(node)

    def scan_calls(self, tree) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # 内置 open(path, mode)
            if isinstance(func, ast.Name) and func.id == "open":
                arg0 = node.args[0] if node.args else None
                if arg0 is not None and self._is_write_mode(self._mode(node)):
                    self.emit(OP_WRITE, "open", arg0, self.resolve(arg0))
                continue

            if not isinstance(func, ast.Attribute):
                continue
            attr = func.attr
            obj = func.value

            # os.*
            if isinstance(obj, ast.Name) and obj.id in self.os_names:
                if attr in _OS_DELETE_ATTRS and node.args:
                    self.emit(OP_DELETE, f"os.{attr}", node.args[0], self.resolve(node.args[0]))
                elif attr in _OS_MKDIR_ATTRS and node.args:
                    self.emit(OP_WRITE, f"os.{attr}", node.args[0], self.resolve(node.args[0]))
                elif attr in _OS_RENAME_ATTRS:
                    if node.args:
                        self.emit(OP_DELETE, f"os.{attr}", node.args[0], self.resolve(node.args[0]))
                    if len(node.args) >= 2:
                        self.emit(OP_WRITE, f"os.{attr}", node.args[1], self.resolve(node.args[1]))
                continue

            # shutil.*
            if isinstance(obj, ast.Name) and obj.id in self.shutil_names:
                if attr in _SHUTIL_DELETE_ATTRS and node.args:
                    self.emit(OP_DELETE, f"shutil.{attr}", node.args[0], self.resolve(node.args[0]))
                elif attr in _SHUTIL_MOVE_ATTRS:
                    if node.args:
                        self.emit(OP_DELETE, f"shutil.{attr}", node.args[0], self.resolve(node.args[0]))
                    if len(node.args) >= 2:
                        self.emit(OP_WRITE, f"shutil.{attr}", node.args[1], self.resolve(node.args[1]))
                elif attr in _SHUTIL_WRITE_ATTRS and node.args:
                    self.emit(OP_WRITE, f"shutil.{attr}", node.args[-1], self.resolve(node.args[-1]))
                continue

            # Path(...).method(...)
            base = self._base_path(obj)
            if base is not None:
                if attr in _PATH_DELETE_ATTRS:
                    self.emit(OP_DELETE, f"Path.{attr}", obj, base)
                elif attr in _PATH_WRITE_ATTRS:
                    self.emit(OP_WRITE, f"Path.{attr}", obj, base)
                elif attr in _PATH_RENAME_ATTRS:
                    self.emit(OP_DELETE, f"Path.{attr}", obj, base)
                    if node.args:
                        self.emit(OP_WRITE, f"Path.{attr}", node.args[0], self.resolve(node.args[0]))
                elif attr == "open" and self._is_write_mode(self._mode(node)):
                    self.emit(OP_WRITE, "Path.open", obj, base)
                continue

            # p.write_text(...)（p 是前面赋值的 Path 变量）
            if isinstance(obj, ast.Name) and obj.id in self.path_vars:
                base = self.symtab.get(obj.id, _DYN)
                if attr in _PATH_DELETE_ATTRS:
                    self.emit(OP_DELETE, "Path." + attr, obj, base)
                elif attr in _PATH_WRITE_ATTRS:
                    self.emit(OP_WRITE, "Path." + attr, obj, base)
                elif attr in _PATH_RENAME_ATTRS:
                    self.emit(OP_DELETE, "Path." + attr, obj, base)
                    if node.args:
                        self.emit(OP_WRITE, "Path." + attr, node.args[0], self.resolve(node.args[0]))
                elif attr == "open" and self._is_write_mode(self._mode(node)):
                    self.emit(OP_WRITE, "Path.open", obj, base)


def collect_python_targets(code: str, cwd: str) -> list[PathTarget]:
    """AST 收集 python 代码中的写/删目标（语法错误返回空列表）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    os_names, shutil_names, path_names, pathlib_mods = _collect_aliases(tree)
    collector = _Collector(code, cwd, os_names, shutil_names, path_names, pathlib_mods)
    collector.collect_symbols(tree)
    collector.scan_calls(tree)
    return collector.targets
