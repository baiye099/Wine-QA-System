"""模型返回及已存储向量共用的值校验。"""

import math

from ..core.errors import ModelError


def validate_vector(vector: list, dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ModelError("Embedding 维度或数值不合法，请核对模型配置。")
    if not any(vector):
        raise ModelError("Embedding 不能为零向量。")
    return [float(v) for v in vector]
