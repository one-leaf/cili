"""工具输出外置：纯文本保存为 .txt，多模态保存为 .json。

由 runner 在工具执行后调用；写入失败不影响工具执行。
"""

from __future__ import annotations

import json
import os
from typing import Any


def find_tool_output_file(
    session_dir,
    output_path: str,
    *,
    workspace_uuid: str = "",
    key: str = "",
):
    """按会话目录 → cache/tool_output 的顺序找输出文件。

    M3 兼容层：旧文件仍在 session_dir；新文件（M7 起）在 cache。
    ``output_path`` 只取文件名，避免会话文件里的路径穿越。
    """
    from pathlib import Path

    name = Path(str(output_path or "")).name
    if not name:
        return None
    session_dir = Path(session_dir)
    candidate = (session_dir / name).resolve()
    if candidate.is_relative_to(session_dir.resolve()) and candidate.is_file():
        return candidate
    if not workspace_uuid or not key:
        return None
    try:
        from core.workspace_layout import tool_output_dir

        root = tool_output_dir(workspace_uuid, key).resolve()
        candidate = (root / name).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except (OSError, ValueError):
        return None
    return None


def save_tool_output(result: Any, output_file: str | None) -> str | None:
    """统一保存工具输出到外部文件，返回实际保存路径（未保存返回 None）。

    如果文件已存在且有内容（_run_bash 实时写入过），则跳过避免覆盖。
    支持纯文本（.txt）与多模态（.json，包含图片和文本块）。
    """
    path = output_file
    if not path:
        return None

    # 检查文件是否已有内容（_run_bash 实时写入）
    if os.path.exists(path):
        try:
            if os.path.getsize(path) > 0:
                return None  # _run_bash 已实时写入，不覆盖
        except Exception:
            pass

    try:
        from core.llm.types import ImageBlock

        # 检查是否有图片块
        has_images = any(isinstance(block, ImageBlock) for block in result.blocks)

        if has_images:
            # 多模态内容：保存为 json（替换 .txt 后缀为 .json）
            if path.endswith(".txt"):
                json_path = path[:-4] + ".json"
            else:
                json_path = path + ".json"
            data = {
                "type": "multimodal",
                "blocks": []
            }
            for block in result.blocks:
                if hasattr(block, 'to_dict'):
                    data["blocks"].append(block.to_dict())
                elif hasattr(block, '__dict__'):
                    # 简单转换
                    block_dict = {}
                    for k, v in block.__dict__.items():
                        if not k.startswith('_'):
                            block_dict[k] = v
                    data["blocks"].append({"type": block.__class__.__name__.lower().replace('block', ''), **block_dict})

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return json_path
        else:
            # 纯文本：保持 txt
            with open(path, "w", encoding="utf-8") as f:
                f.write(result.output)
            return path
    except Exception:
        return None  # 写入失败不影响工具执行


def resolve_tool_results_for_context(
    messages: list[dict], session_dir, *, workspace_uuid: str = "", key: str = ""
) -> None:
    """为上下文估算解析外置工具结果（不改 UI 渲染路径）。

    与 runner 的 ``_resolve_tool_results`` 同口径：
    - ``.json`` 多模态文件解析成 blocks（图片随后由图片投影替换为引用）
    - ``.txt`` 截断结果按 8K 中间截断，完整结果按通用 50K 字符上限
    - M3 兼容：session_dir 找不到时回退 cache/tool_output
    """
    from pathlib import Path

    from core.compression import truncate_middle, truncate_result
    from core.llm.types import block_from_dict
    from core.tool_limits import TOOL_RESULT_MAX_CHARS

    session_dir = Path(session_dir)
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            meta = block.get("_meta") or {}
            output_path = str(meta.get("output_path") or "")
            if not output_path or block.get("content"):
                continue
            file_path = find_tool_output_file(
                session_dir, output_path, workspace_uuid=workspace_uuid, key=key
            )
            if file_path is None:
                continue
            try:
                if file_path.suffix.lower() == ".json":
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or data.get("type") != "multimodal":
                        block["content"] = "[工具输出格式错误]"
                        continue
                    blocks = []
                    for block_data in data.get("blocks", []):
                        try:
                            parsed = block_from_dict(block_data)
                            blocks.append(parsed.to_dict() if hasattr(parsed, "to_dict") else parsed)
                        except Exception:
                            continue
                    block["content"] = blocks
                else:
                    text = file_path.read_text(encoding="utf-8", errors="replace")
                    if meta.get("truncated"):
                        block["content"] = truncate_middle(text, 8000)
                    else:
                        block["content"] = truncate_result(text, TOOL_RESULT_MAX_CHARS)
            except Exception:
                continue


def read_tool_output_chunk(session_dir, tool_use_id: str, offset: int,
                           max_chunk: int = 256 * 1024, *,
                           workspace_uuid: str = "", key: str = "") -> dict:
    """读取工具输出文件的新增内容（前端轮询实时显示）。

    文件命名 ``{tool_use_id}.txt`` / ``{tool_use_id}_{tool_name}.txt``，
    兼容 exec_* 子目录和 cache/tool_output。返回 ``{content, offset, exists}``。
    """
    matches = list(session_dir.glob(f"{tool_use_id}.txt")) + list(session_dir.glob(f"{tool_use_id}_*.txt"))
    for exec_dir in session_dir.glob("exec_*"):
        if exec_dir.is_dir():
            matches.extend(exec_dir.glob(f"{tool_use_id}.txt"))
            matches.extend(exec_dir.glob(f"{tool_use_id}_*.txt"))
    if not matches and workspace_uuid and key:
        try:
            from core.workspace_layout import tool_output_dir
            cache_dir = tool_output_dir(workspace_uuid, key)
            matches = list(cache_dir.glob(f"{tool_use_id}.txt"))
            matches += list(cache_dir.glob(f"{tool_use_id}_*.txt"))
        except OSError:
            matches = []

    if not matches:
        return {"content": "", "offset": 0, "exists": False}

    output_file = matches[0]
    file_size = output_file.stat().st_size
    if offset >= file_size:
        return {"content": "", "offset": file_size, "exists": True}

    with open(output_file, "rb") as f:
        f.seek(offset)
        chunk = f.read(max_chunk)

    return {
        "content": chunk.decode("utf-8", "replace"),
        "offset": offset + len(chunk),
        "exists": True,
    }
