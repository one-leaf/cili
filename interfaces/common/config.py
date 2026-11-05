"""接入端账号配置共享辅助：宽松类型转换。"""

from __future__ import annotations

INVALID = object()   # 类型不合法的哨兵


def coerce_value(type_name, value):
    """按字段声明的类型做宽松转换；无法转换返回 ``INVALID``。

    ``from __future__ import annotations`` 生效时 ``field.type`` 是字符串，
    所以两种形态都要处理。
    """
    name = type_name if isinstance(type_name, str) else getattr(type_name, "__name__", "")
    if isinstance(value, bool) and name in ("int", "float"):
        return INVALID            # True/False 不是合法的数字配置
    try:
        if name == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(value, (int, float)):
                return bool(value)
            return INVALID
        if name == "int":
            return int(value)
        if name == "float":
            return float(value)
        if name == "str":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return INVALID
    return value
