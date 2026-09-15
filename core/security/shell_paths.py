"""shell 命令写/删目标提取：bash/pwsh 路径权限门。

按命令静态提取可能的写/删目标（PathTarget 列表），交给 PathPolicy 判定：
区内放行、区外未批待审、无 store fail-closed。动态目标（变量/通配符/
命令替换）无法静态解析，保守按越界待审。读取类操作（cat/ls/`<` 重定向）
不收集。

保守原则：宁可多审不放过——无法确定是否写/删的目标一律收集，命中审批
（"允许并记住"可缓解）。误判只会多一次审批卡，不会绕过安全。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from core.security.path_policy import OP_DELETE, OP_WRITE, PathTarget


@dataclass
class _Tok:
    kind: str  # "word" | "redir"
    text: str
    dynamic: bool = False


# ─── bash 命令操作表 ─────────────────────────────────────────────
# 键：小写命令名；值：(语义, 目标规则)
#   "delete"  + "all"    → 全部操作数 delete
#   "write"   + "all"    → 全部操作数 write
#   "write"   + "last"   → 最后一个操作数 write（cp/ln 的目标）
#   "write"   + "of="    → 以 of= 开头的操作数 write（dd）
#   "mixed"              → mv：除最后一个外 delete，最后一个 write
_BASH_OPS = {
    "rm": ("delete", "all"),
    "rmdir": ("delete", "all"),
    "unlink": ("delete", "all"),
    "mv": ("mixed", None),
    "cp": ("write", "last"),
    "install": ("write", "last"),
    "ln": ("write", "last"),
    "tee": ("write", "all"),
    "truncate": ("write", "all"),
    "touch": ("write", "all"),
    "mkdir": ("write", "all"),
    "dd": ("write", "of="),
}

_BASH_VALUE_FLAGS = {"-t", "--target-directory"}

# ─── pwsh 命令操作表 ─────────────────────────────────────────────
# 值：(语义, 源参数, 目标参数)。源参数值 → delete（如 Remove-Item -Path、
# Move-Item -Path）；目标参数值 → write（Move-Item -Destination、
# Copy-Item -Destination）。位置操作数按语义处理。
_PWSH_OPS = {
    "remove-item": ("delete", ["-Path", "-LiteralPath"], []),
    "move-item": ("mixed", ["-Path", "-LiteralPath"], ["-Destination"]),
    "copy-item": ("write-dest", ["-Path", "-LiteralPath"], ["-Destination"]),
    "new-item": ("write", ["-Path", "-Name"], []),
    "mkdir": ("write", ["-Path", "-Name"], []),
    "md": ("write", ["-Path", "-Name"], []),
    "set-content": ("write", ["-Path", "-LiteralPath"], []),
    "add-content": ("write", ["-Path", "-LiteralPath"], []),
    "out-file": ("write", ["-FilePath", "-Path"], []),
    "export-csv": ("write", ["-Path", "-LiteralPath"], []),
}

_PWSH_ALIASES = {
    "ri": "remove-item", "rm": "remove-item", "del": "remove-item",
    "erase": "remove-item", "rd": "remove-item", "rmdir": "remove-item",
    "mi": "move-item",
    "ci": "copy-item", "cp": "copy-item",
    "ni": "new-item",
    "sc": "set-content",
    "ac": "add-content",
}

_CD_CMDS = {"cd", "pushd", "set-location", "sl", "push-location"}
_CD_RESTORE = {"popd", "pop-location"}
_CD_CMDS |= _CD_CMDS  # noqa: PLW0127 保持显式

_DEV_NULLISH = ("/dev/null", "/dev/tty", "/dev/stdout", "/dev/stderr", "nul", "con", "com1", "lpt1")
_GIT_BASH_DRIVE = re.compile(r"^/([a-zA-Z])/(.*)$")
# Windows 盘符起始（C:\ 或 C:/）。词形到该前缀时反斜杠是路径分隔符而非转义。
_WIN_DRIVE_START = re.compile(r"^[A-Za-z]:[\\/]")


def _is_ignored_target(text: str) -> bool:
    """fd 重定向 / 设备文件不当作写目标。"""
    return text in _DEV_NULLISH or text.startswith("&") or text.isdigit()


def _split_segments(command: str, mode: str) -> list[str]:
    """按顶层分隔符切段（; && || |），忽略引号内与括号内的内容。"""
    segments: list[str] = []
    cur: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    depth = 0
    while i < n:
        c = command[i]
        if quote is not None:
            if c == quote:
                if mode == "pwsh" and i + 1 < n and command[i + 1] == quote:
                    cur.append(command[i:i + 2]); i += 2; continue
                quote = None
            cur.append(c); i += 1; continue
        if c in "'\"":
            quote = c; cur.append(c); i += 1; continue
        if c == "(":
            depth += 1; cur.append(c); i += 1; continue
        if c == ")":
            depth = max(0, depth - 1); cur.append(c); i += 1; continue
        if depth == 0 and c == "#":
            break  # 注释：其后全部忽略
        if depth == 0 and c in ";&|":
            two = command[i:i + 2]
            if two in ("&&", "||"):
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []; i += 2; continue
            seg = "".join(cur).strip()
            if seg:
                segments.append(seg)
            cur = []; i += 1; continue
        cur.append(c); i += 1
    seg = "".join(cur).strip()
    if seg:
        segments.append(seg)
    return segments


def _lex(segment: str, mode: str) -> list[_Tok]:
    """把一段命令切成 word / redir 记号，跟踪引号与动态标记。

    动态标记（dynamic=True）：$、反引号、命令替换、通配符、{、,（数组）——
    无法静态解析为单个字面路径，保守按越界待审。
    """
    tokens: list[_Tok] = []
    buf: list[str] = []
    dynamic = False
    in_word = False
    quote: str | None = None
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if quote is not None:
            if c == quote:
                if mode == "pwsh" and i + 1 < n and segment[i + 1] == quote:
                    buf.append(quote); i += 2; continue
                quote = None; i += 1; continue
            if quote == '"' and c == "\\" and mode == "bash" and i + 1 < n:
                nxt = segment[i + 1]
                if nxt in '"\\$`':
                    buf.append(nxt); i += 2; continue
                buf.append(c); i += 1; continue  # bash 双引号内其余反斜杠保持字面
            if quote == '"' and c == "`" and mode == "pwsh" and i + 1 < n:
                buf.append(segment[i + 1]); i += 2; continue
            if c in "$`":
                dynamic = True
            buf.append(c); i += 1; continue
        if c in "'\"":
            quote = c; in_word = True; i += 1; continue
        if c == "\\" and mode == "bash" and i + 1 < n:
            if _WIN_DRIVE_START.match("".join(buf) + "\\"):
                buf.append(c); i += 1; continue  # Windows 盘符路径，反斜杠保持字面
            buf.append(segment[i + 1]); i += 2; in_word = True; continue
        if c == "`" and mode == "pwsh" and i + 1 < n:
            buf.append(segment[i + 1]); i += 2; in_word = True; continue
        if c == "`" and mode == "bash":
            j = segment.find("`", i + 1)
            if j == -1:
                dynamic = True; i += 1; in_word = True; continue
            buf.append(segment[i:j + 1]); i = j + 1; dynamic = True; in_word = True; continue
        if c.isspace():
            if in_word:
                tokens.append(_Tok("word", "".join(buf), dynamic))
                buf = []; dynamic = False; in_word = False
            i += 1; continue
        if c in "$":
            dynamic = True; buf.append(c); i += 1; in_word = True; continue
        if c in "*?[{,":
            dynamic = True; buf.append(c); i += 1; in_word = True; continue
        if mode == "pwsh" and c == "]":
            dynamic = True; buf.append(c); i += 1; in_word = True; continue
        if c in "><":
            if in_word:
                tokens.append(_Tok("word", "".join(buf), dynamic))
                buf = []; dynamic = False; in_word = False
            if c == ">" and i + 1 < n and segment[i + 1] == ">":
                tokens.append(_Tok("redir", ">>")); i += 2; continue
            tokens.append(_Tok("redir", c)); i += 1; continue
        buf.append(c); i += 1; in_word = True
    if in_word:
        tokens.append(_Tok("word", "".join(buf), dynamic))
    return tokens


def normalize_shell_path(raw: str, cur: str) -> str:
    """把 shell 字面量路径归一化为 Windows 绝对路径。

    Windows 关键：Git Bash 盘符写法 /e/... → E:\\...，否则 realpath 会拼到
    当前盘根。`~`/`~+` 展开；相对路径 join cur 后 realpath。
    """
    text = raw.strip()
    if text in ("", "."):
        return os.path.realpath(cur)
    if text == "~":
        return os.path.realpath(os.path.expanduser("~"))
    if text.startswith("~/"):
        text = os.path.join(os.path.expanduser("~"), text[2:])
    elif text == "~+":
        text = cur
    m = _GIT_BASH_DRIVE.match(text)
    if m:
        text = f"{m.group(1).upper()}:\\{m.group(2).replace('/', os.sep)}"
    if not os.path.isabs(text):
        text = os.path.join(cur, text)
    return os.path.realpath(text)


def _make_target(op: str, tok: _Tok, op_name: str, cur: str, cwd_unknown: bool) -> PathTarget:
    if tok.dynamic or (cwd_unknown and not os.path.isabs(tok.text)):
        return PathTarget(op, tok.text, op_name=op_name, dynamic=True)
    return PathTarget(op, tok.text, op_name=op_name, resolved=normalize_shell_path(tok.text, cur))


def _bash_operands(tokens: list[_Tok]) -> tuple[list[_Tok], list[_Tok]]:
    """去标志取操作数；-t/--target-directory 的值单独收进 extras（写目标）。"""
    positionals: list[_Tok] = []
    extras: list[_Tok] = []
    i, n = 0, len(tokens)
    stop_flags = False
    while i < n:
        tok = tokens[i]
        if tok.kind == "redir":
            i += 1; continue
        text = tok.text
        if not stop_flags and text == "--":
            stop_flags = True; i += 1; continue
        if not stop_flags and text.startswith("-") and text != "-":
            if text in _BASH_VALUE_FLAGS and i + 1 < n:
                extras.append(tokens[i + 1]); i += 2; continue
            i += 1; continue
        if text in ("-", "&"):
            i += 1; continue
        positionals.append(tok); i += 1
    return positionals, extras


def _bash_targets(cmd: str, tokens: list[_Tok], cur: str, cwd_unknown: bool) -> list[PathTarget]:
    spec = _BASH_OPS.get(cmd)
    targets: list[PathTarget] = []
    if spec:
        kind, how = spec
        positionals, extras = _bash_operands(tokens)
        if kind == "delete":
            for tok in positionals + extras:
                targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
        elif kind == "write" and how == "all":
            for tok in positionals + extras:
                targets.append(_make_target(OP_WRITE, tok, cmd, cur, cwd_unknown))
        elif kind == "write" and how == "last":
            if extras:
                targets.append(_make_target(OP_WRITE, extras[-1], cmd, cur, cwd_unknown))
            elif positionals:
                targets.append(_make_target(OP_WRITE, positionals[-1], cmd, cur, cwd_unknown))
        elif kind == "write" and how == "of=":
            for tok in positionals:
                if tok.text.startswith("of="):
                    val = tok.text[3:]
                    targets.append(_make_target(OP_WRITE, _Tok("word", val, tok.dynamic), cmd, cur, cwd_unknown))
        elif kind == "mixed":
            if extras:
                for tok in positionals:
                    targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
                targets.append(_make_target(OP_WRITE, extras[-1], cmd, cur, cwd_unknown))
            elif len(positionals) >= 2:
                for tok in positionals[:-1]:
                    targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
                targets.append(_make_target(OP_WRITE, positionals[-1], cmd, cur, cwd_unknown))
            elif positionals:
                targets.append(_make_target(OP_WRITE, positionals[0], cmd, cur, cwd_unknown))
    return targets


def _pwsh_named(tokens: list[_Tok], source_params: list[str], dest_params: list[str]):
    positionals: list[_Tok] = []
    source_vals: list[_Tok] = []
    dest_vals: list[_Tok] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind == "redir":
            i += 1; continue
        text = tok.text
        if text.startswith("-") and len(text) > 1:
            if text in source_params and i + 1 < n:
                source_vals.append(tokens[i + 1]); i += 2; continue
            if text in dest_params and i + 1 < n:
                dest_vals.append(tokens[i + 1]); i += 2; continue
            i += 1; continue
        if text in ("-", "&"):
            i += 1; continue
        positionals.append(tok); i += 1
    return positionals, source_vals, dest_vals


def _pwsh_targets(cmd: str, tokens: list[_Tok], cur: str, cwd_unknown: bool) -> list[PathTarget]:
    spec = _PWSH_OPS.get(cmd)
    if not spec:
        return []
    kind, source_params, dest_params = spec
    positionals, source_vals, dest_vals = _pwsh_named(tokens, source_params, dest_params)
    targets: list[PathTarget] = []
    if kind == "delete":
        for tok in positionals + source_vals:
            targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
    elif kind == "write":
        # 写操作多路径罕见，取第一个位置操作数即可避免把值当路径误报
        for tok in source_vals + dest_vals:
            targets.append(_make_target(OP_WRITE, tok, cmd, cur, cwd_unknown))
        if not source_vals and not dest_vals and positionals:
            targets.append(_make_target(OP_WRITE, positionals[0], cmd, cur, cwd_unknown))
    elif kind == "write-dest":
        if dest_vals:
            for tok in dest_vals:
                targets.append(_make_target(OP_WRITE, tok, cmd, cur, cwd_unknown))
        elif positionals:
            targets.append(_make_target(OP_WRITE, positionals[-1], cmd, cur, cwd_unknown))
    elif kind == "mixed":
        if dest_vals:
            for tok in source_vals + positionals:
                targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
            for tok in dest_vals:
                targets.append(_make_target(OP_WRITE, tok, cmd, cur, cwd_unknown))
        elif positionals:
            for tok in positionals[:-1]:
                targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
            targets.append(_make_target(OP_WRITE, positionals[-1], cmd, cur, cwd_unknown))
        else:
            for tok in source_vals:
                targets.append(_make_target(OP_DELETE, tok, cmd, cur, cwd_unknown))
    return targets


def _redirect_targets(tokens: list[_Tok], cmd: str, cur: str, cwd_unknown: bool) -> list[PathTarget]:
    """`>`/`>>` 重定向 → 下个 word 为写目标；fd/设备目标忽略。"""
    targets: list[PathTarget] = []
    for j, tok in enumerate(tokens):
        if tok.kind == "redir" and tok.text.startswith(">"):
            for k in range(j + 1, len(tokens)):
                nxt = tokens[k]
                if nxt.kind == "word":
                    if _is_ignored_target(nxt.text):
                        break
                    targets.append(_make_target(OP_WRITE, nxt, cmd, cur, cwd_unknown))
                    break
    return targets


def _cd_target(tokens: list[_Tok], mode: str) -> _Tok | None:
    for tok in tokens:
        if tok.kind != "word":
            continue
        text = tok.text
        if text in ("--", "-", "&"):
            continue
        if text.startswith("-"):
            continue  # 跳过 -Path 等标志，其值即下一 word
        return tok
    return None


def extract_shell_targets(command: str, mode: str, cwd: str, initial_dir: str) -> list[PathTarget]:
    """提取命令中的写/删目标。

    mode ∈ {"bash", "pwsh"}。cwd 为工作区根（相对路径解析基准）；
    initial_dir 为命令起始目录（= cwd 或 working_dir 覆盖）。返回
    PathTarget 列表（可为空），交 PathPolicy 判定。
    """
    segments = _split_segments(command, mode)
    targets: list[PathTarget] = []
    cur = os.path.abspath(initial_dir)
    cwd_unknown = False
    for seg in segments:
        tokens = _lex(seg, mode)
        if not tokens or tokens[0].kind != "word":
            continue
        cmd = tokens[0].text.lower()
        if cmd in _CD_CMDS:
            target = _cd_target(tokens[1:], mode)
            if target is None:
                continue
            if target.dynamic:
                cwd_unknown = True
            else:
                cur = normalize_shell_path(target.text, cur)
            continue
        if cmd in _CD_RESTORE:
            cwd_unknown = True
            continue
        if mode == "bash":
            targets.extend(_bash_targets(cmd, tokens[1:], cur, cwd_unknown))
        else:
            targets.extend(_pwsh_targets(cmd, tokens[1:], cur, cwd_unknown))
        targets.extend(_redirect_targets(tokens[1:], cmd, cur, cwd_unknown))
    return targets
