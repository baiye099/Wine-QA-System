"""阻止额外字段与非有限数值进入结构化数据。"""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
