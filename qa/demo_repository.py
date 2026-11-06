"""显式离线演示适配器；仅用正式清洗记录，不伪造数据库或语义召回。"""

from copy import deepcopy

from database.dataset import Dataset

from .config import Settings
from .errors import KnowledgeNotReady
from .filtering import eligible_price, matches_filters
from .schemas import EntityRef, KnowledgeChunk, ProductFilters, Source


def chunk_from_record(row: dict, score: float | None = None) -> KnowledgeChunk:
    keys = ("knowledge_id", "title", "text", "fact_scope", "category", "entity_ids", "source_ids", "text_type")
    return KnowledgeChunk(**{key: row[key] for key in keys}, score=score)


class DemoRepository:
    def __init__(self, dataset: Dataset, settings: Settings):
        self.dataset, self.settings = dataset, settings
        self.catalog = {r["entity_id"]: r for r in dataset.catalog}
        self.product_map = {r["product_id"]: r for r in dataset.tables["products"]}
        self.winery_map = {r["winery_id"]: r for r in dataset.tables["wineries"]}
        self.source_map = {r["source_id"]: r for r in dataset.tables["sources"]}

    def ensure_ready(self) -> None:
        pass

    def aliases(self) -> list[dict]:
        return [{"alias": r["alias"], "entity_id": r["entity_id"], "entity_type": r["entity_type"],
                 "name": self.catalog[r["entity_id"]]["name"]} for r in self.dataset.tables["aliases"]]

    def entities(self, entity_ids: list[str]) -> list[EntityRef]:
        return [EntityRef(**{k: self.catalog[e][k] for k in ("entity_id", "entity_type", "name")}) for e in dict.fromkeys(entity_ids) if e in self.catalog]

    def products(self, product_ids: list[str]) -> list[dict]:
        rows = []
        for pid in dict.fromkeys(product_ids):
            if pid not in self.product_map:
                continue
            row = deepcopy(self.product_map[pid])
            row["winery_name"] = self.winery_map[row["winery_id"]]["name"]
            row["region_name"] = self.catalog.get(row.get("region_id"), {}).get("name")
            prices = [r for r in self.dataset.tables["price_observations"] if r["product_id"] == pid and eligible_price(r, row, self.settings)]
            if prices:
                row["eligible_price"] = max(prices, key=lambda r: (r["verified_at"], r["price_id"]))
            rows.append(row)
        return rows

    def wineries(self, winery_ids: list[str]) -> list[dict]:
        return [deepcopy(self.winery_map[w]) for w in dict.fromkeys(winery_ids) if w in self.winery_map]

    def filter_products(self, filters: ProductFilters, product_ids: list[str] | None, limit: int) -> list[dict]:
        ids = sorted(self.product_map) if product_ids is None else product_ids
        return [p for p in self.products(ids) if matches_filters(p, filters)][:limit]

    def sources(self, source_ids: list[str]) -> list[Source]:
        keys = ("source_id", "title", "url", "publisher", "evidence_form")
        return [Source(**{k: self.source_map[s].get(k) for k in keys}) for s in dict.fromkeys(source_ids) if s in self.source_map]

    def related(self, entity_ids: list[str], limit: int = 3) -> list[KnowledgeChunk]:
        return [chunk_from_record(r) for r in self.dataset.tables["knowledge"]
                if r["retrieval_allowed"] and set(entity_ids).intersection(r["entity_ids"])][:limit]

    def ensure_vector_ready(self) -> None:
        raise KnowledgeNotReady("离线演示未连接 Neo4j 向量索引，知识语义问答尚未就绪。")

    def search(self, vector: list[float], entity_ids: list[str], category: str | None, limit: int) -> list[KnowledgeChunk]:
        self.ensure_vector_ready()
        return []

    def close(self) -> None:
        pass

