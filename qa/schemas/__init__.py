"""请求、响应与模型返回的 Pydantic 契约。"""

from .base import Contract
from .query import BusinessID, EntityRef, EntityType, FilterName, Intent, ParsedQuery, ProductFilters
from .response import KnowledgeChunk, QAResponse, Source

__all__ = [
    "Contract", "BusinessID", "EntityRef", "EntityType", "FilterName", "Intent",
    "ParsedQuery", "ProductFilters", "KnowledgeChunk", "QAResponse", "Source",
]
