"""工具输出外置：纯文本保存为 .txt，多模态保存为 .json。

由 runner 在工具执行后调用；写入失败不影响工具执行。
"""

from __future__ import annotations

import json
import os
from typing import Any


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


def read_tool_output_chunk(session_dir, tool_use_id: str, offset: int,
                           max_chunk: int = 256 * 1024) -> dict:
    """读取工具输出文件的新增内容（前端轮询实时显示）。

    文件命名 ``{tool_use_id}.txt`` / ``{tool_use_id}_{tool_name}.txt``，
    兼容 exec_* 子目录。返回 ``{content, offset, exists}``。
    """
    matches = list(session_dir.glob(f"{tool_use_id}.txt")) + list(session_dir.glob(f"{tool_use_id}_*.txt"))
    for exec_dir in session_dir.glob("exec_*"):
        if exec_dir.is_dir():
            matches.extend(exec_dir.glob(f"{tool_use_id}.txt"))
            matches.extend(exec_dir.glob(f"{tool_use_id}_*.txt"))

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
