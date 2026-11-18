"""兼容 Chat Completions 与 Embeddings 的 HTTP 适配器。"""

from __future__ import annotations

from ..core.config import Settings
from ..core.errors import ModelError
from ..models.vector import validate_vector


class CompatibleHTTP:
    def __init__(self, base_url: str, api_key: str, timeout: float):
        self.base_url, self.api_key, self.timeout = base_url.rstrip("/"), api_key, timeout

    def post(self, path: str, payload: dict) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.post(self.base_url + path, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("expected object")
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError("模型接口调用失败，请检查地址、模型名、鉴权和兼容协议。") from exc


class HTTPChatModel(CompatibleHTTP):
    def __init__(self, settings: Settings):
        super().__init__(settings.llm_base_url, settings.llm_api_key.get_secret_value(), settings.model_timeout_seconds)
        self.model = settings.llm_model

    def complete_json(self, messages: list[dict[str, str]]) -> str:
        response = self.post("/chat/completions", {"model": self.model, "messages": messages, "temperature": 0,
                                                   "response_format": {"type": "json_object"}})
        try:
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            return content
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("大模型返回格式不符合 Chat 接口约定。") from exc


class HTTPEmbeddingModel(CompatibleHTTP):
    def __init__(self, settings: Settings):
        super().__init__(settings.embedding_base_url, settings.embedding_api_key.get_secret_value(), settings.model_timeout_seconds)
        self.model, self.dimensions = settings.embedding_model, settings.embedding_dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.post("/embeddings", {"model": self.model, "input": texts, "encoding_format": "float"})
        try:
            rows = sorted(response["data"], key=lambda r: r["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise ValueError("embedding indexes")
            return [validate_vector(r["embedding"], self.dimensions) for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("Embedding 接口返回数量、顺序或格式不正确。") from exc

