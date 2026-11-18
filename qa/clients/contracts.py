"""业务层使用的模型接口。"""

from typing import Protocol


class ChatModel(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> str: ...


class EmbeddingModel(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
