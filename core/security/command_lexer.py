"""shell 命令词法：分段、切词与动态标记（bash/pwsh 共用）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Windows 盘符起始（C:\ 或 C:/）。词形到该前缀时反斜杠是路径分隔符而非转义。
_WIN_DRIVE_START = re.compile(r"^[A-Za-z]:[\\/]")

@dataclass
class _Tok:
    kind: str  # "word" | "redir"
    text: str
    dynamic: bool = False


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
