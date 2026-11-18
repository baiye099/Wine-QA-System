"""问句、别名及数据导出共用的文本规范化。"""

import re


def normalized_alias(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()
