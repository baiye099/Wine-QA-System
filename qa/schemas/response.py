"""问答结果、来源及知识证据的结构化契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Contract
from .query import Intent, ProductFilters


class KnowledgeChunk(Contract):
    knowledge_id: str
    title: str
    text: str
    fact_scope: str
    category: str
    entity_ids: list[str]
    source_ids: list[str]
    text_type: str = "faithful_summary"
    score: float | None = None

    @classmethod
    def from_record(cls, row: dict, score: float | None = None) -> KnowledgeChunk:
        keys = ("knowledge_id", "title", "text", "fact_scope", "category", "entity_ids", "source_ids", "text_type")
        return cls(**{key: row[key] for key in keys}, score=score)


class Source(Contract):
    source_id: str
    title: str
    url: str | None = None
    publisher: str | None = None
    evidence_form: str


class QAResponse(Contract):
    status: Literal["ok", "empty", "clarification", "not_ready", "error"]
    text: str
    intent: Intent | None = None
    products: list[dict] = Field(default_factory=list)
    wineries: list[dict] = Field(default_factory=list)
    evidence: list[KnowledgeChunk] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    effective_filters: ProductFilters = Field(default_factory=ProductFilters)
    active_entity_ids: list[str] = Field(default_factory=list)

    @property
    def product_ids(self) -> list[str]:
        return [p["product_id"] for p in self.products]


class EvidenceClaim(Contract):
    text: str = Field(min_length=1, max_length=800)
    knowledge_ids: list[str] = Field(min_length=1, max_length=5)


class EvidenceAnswer(Contract):
    claims: list[EvidenceClaim] = Field(min_length=1, max_length=5)
