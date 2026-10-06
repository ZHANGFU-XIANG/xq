"""共用小工具。"""
from __future__ import annotations

import math


def json_safe(obj):
    """把 NaN／Infinity 換成 None（JSON 不允許這些值；資料源偶爾回傳缺值，不能讓整份報告或 API 壞掉）。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):  # numpy 純量
        try:
            return json_safe(obj.item())
        except (TypeError, ValueError):
            return obj
    return obj
