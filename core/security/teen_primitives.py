"""青少年模式的底层原语：密码哈希验证与可用时段判断。"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def _hash_password(password: str, salt: bytes | None = None) -> dict:
    """scrypt 加盐哈希（不存明文）。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return {"algo": "scrypt", "salt": salt.hex(), "hash": digest.hex(),
            "n": 16384, "r": 8, "p": 1}


def _verify_password(password: str, record: dict) -> bool:
    try:
        expected = bytes.fromhex(record.get("hash", ""))
        salt = bytes.fromhex(record.get("salt", ""))
    except ValueError:
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(record.get("n", 16384)), r=int(record.get("r", 8)),
            p=int(record.get("p", 1)), dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, OSError):
        return False
    return hmac.compare_digest(digest, expected)


def _parse_windows(windows: list) -> list[tuple[int, int]]:
    """解析 ["08:00-21:00", ...] → [(分钟数, 分钟数)]；非法段忽略。"""
    result = []
    for w in windows or []:
        try:
            a, b = str(w).split("-", 1)
            ha, ma = a.strip().split(":", 1)
            hb, mb = b.strip().split(":", 1)
            start = int(ha) * 60 + int(ma)
            end = int(hb) * 60 + int(mb)
            if 0 <= start <= 1440 and 0 <= end <= 1440:
                result.append((start, end))
        except (ValueError, AttributeError):
            continue
    return result


def _in_window(now_minute: int, windows: list[tuple[int, int]]) -> bool:
    for start, end in windows:
        if start <= end:
            if start <= now_minute < end:
                return True
        else:  # 跨 midnight，如 22:00-07:00
            if now_minute >= start or now_minute < end:
                return True
    return False
