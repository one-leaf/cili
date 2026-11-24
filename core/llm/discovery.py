"""模型列表探测：按协议从网关拉取 /models（分页/异常由调用方决定提示）。"""

from __future__ import annotations

import httpx


async def fetch_model_ids(url: str, headers: dict[str, str], protocol: str) -> list[str]:
    """返回网关声明的模型 ID 列表（anthropic 跟随分页）。"""
    model_ids: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        if protocol == "anthropic":
            params: dict[str, str] = {"limit": "100"}
            while True:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                model_ids.extend(item.get("id", "") for item in data.get("data", []) if item.get("id"))
                if not data.get("has_more") or not data.get("last_id"):
                    break
                params = {"limit": "100", "after_id": data["last_id"]}
        else:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            model_ids.extend(item.get("id", "") for item in data.get("data", []) if item.get("id"))
    return model_ids
