"""SIWC 模型目录解析：兼容标准 /v1/models 与 ChatGPT 目录形状。"""

from __future__ import annotations


def parse_models_payload(data: dict) -> list[dict]:
    """解析模型列表。

    标准 OpenAI：``{"data": [{"id": ...}]}``；
    ChatGPT 目录：``{"models": [{"slug": ..., "visibility": "list"}]}``。
    解析失败返回空列表，由调用方提示手动添加。
    """
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()

    entries = data.get("data")
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id and model_id not in seen:
                seen.add(model_id)
                out.append({
                    "id": model_id,
                    "display_name": str(item.get("display_name") or ""),
                    "context_window": item.get("context_window"),
                })

    if not out:
        entries = data.get("models")
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if item.get("visibility") not in (None, "list"):
                    continue
                slug = item.get("slug")
                if isinstance(slug, str) and slug and slug not in seen:
                    seen.add(slug)
                    out.append({
                        "id": slug,
                        "display_name": str(item.get("display_name") or ""),
                        "context_window": item.get("context_window"),
                    })

    return out
