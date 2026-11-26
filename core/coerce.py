"""通用宽松类型转换：把配置里的原始值按字段声明类型转换。

原属 ``interfaces/common/coerce.py``；邮件账号模型下沉到 core 后，core 也需要
这套转换，故迁移到这里，接入端改为 re-export。
"""

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
            # JSON null 表示"清空"，不能落成字符串 "None"（会被 is_ready 误判为已配置）
            if value is None:
                return ""
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return INVALID
    return value