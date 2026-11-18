"""每个浏览器会话持有自己的状态；业务层不引用 Streamlit。"""

from dataclasses import dataclass, field

from ..schemas.query import ProductFilters
from ..schemas.response import QAResponse


@dataclass
class ConversationState:
    messages: list[dict[str, str]] = field(default_factory=list)
    current_topic: str | None = None
    active_entity_ids: list[str] = field(default_factory=list)
    filters: ProductFilters = field(default_factory=ProductFilters)
    preferences: list[str] = field(default_factory=list)
    last_results: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.messages.clear()
        self.current_topic = None
        self.active_entity_ids.clear()
        self.filters = ProductFilters()
        self.preferences.clear()
        self.last_results.clear()

    def record(self, question: str, response: QAResponse, preferences: list[str]) -> None:
        self.messages.extend([{"role": "user", "content": question}, {"role": "assistant", "content": response.text}])
        if response.status not in {"ok", "empty"} and not (response.status == "not_ready" and response.intent in {"filter", "knowledge"}):
            return
        self.current_topic = response.intent
        self.active_entity_ids = list(response.active_entity_ids)
        self.filters = response.effective_filters.model_copy(deep=True)
        self.preferences = list(preferences) if response.intent == "filter" else []
        self.last_results = list(response.product_ids)

    def context(self) -> dict:
        return {"current_topic": self.current_topic, "active_entity_ids": self.active_entity_ids,
                "filters": self.filters.active(), "preferences": self.preferences, "last_results": self.last_results}

