"""宁夏葡萄酒问答的 Python 调用入口。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bootstrap import build_service
    from .core.config import Settings
    from .models.conversation import ConversationState

__version__ = "0.2.0"
__all__ = ["Settings", "ConversationState", "build_service", "__version__"]


def __getattr__(name: str):
    # 离线 SQL 与图数据工具可使用 QA 包内的共享数据代码，仅需标准库。
    if name == "Settings":
        from .core.config import Settings

        return Settings
    if name == "ConversationState":
        from .models.conversation import ConversationState

        return ConversationState
    if name == "build_service":
        from .bootstrap import build_service

        return build_service
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

