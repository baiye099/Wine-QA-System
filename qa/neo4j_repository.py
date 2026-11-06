"""图关系与知识向量查询；实体、关系、向量均限制发布归属及快照。"""

from __future__ import annotations

from database.dataset import Dataset

from .config import Settings
from .demo_repository import chunk_from_record
from .errors import BackendUnavailable, DataVersionError, KnowledgeNotReady, ModelError
from .models import validate_vector
from .schemas import KnowledgeChunk

CHUNK_SCOPE = """
k.qa_dataset = $dataset AND k.qa_snapshot = $snapshot
AND k.retrieval_allowed = true AND k.contains_price = false
AND k.review_status = 'accepted'
"""
VECTOR_SCOPE = """
k.embedding_model = $model AND k.embedding_dimensions = $dimensions
AND k.embedding_profile = $profile AND k.embedding_text_hash = k.text_hash
AND size(k.embedding) = $dimensions
"""


def validate_index(row: dict, settings: Settings, require_online: bool = True) -> None:
    config = row.get("options", {}).get("indexConfig", {})
    # Neo4j 2026.x 的索引元数据可能返回 COSINE；相似度名称不区分大小写。
    similarity = config.get("vector.similarity_function")
    cosine = isinstance(similarity, str) and similarity.casefold() == "cosine"
    if row.get("labelsOrTypes") != ["Chunk"] or row.get("properties") != ["embedding"] or config.get("vector.dimensions") != settings.embedding_dimensions or not cosine:
        raise KnowledgeNotReady("同名向量索引的标签、属性、维度或相似度与配置不一致，请离线处理。")
    if require_online and row.get("state") != "ONLINE":
        raise KnowledgeNotReady("Neo4j 向量索引尚未 ONLINE，请等待离线构建完成。")


class Neo4jRepository:
    def __init__(self, settings: Settings, dataset: Dataset):
        self.settings, self.dataset, self._driver = settings, dataset, None
        self.chunk_map = {r["knowledge_id"]: r for r in dataset.tables["knowledge"]}

    @property
    def parameters(self) -> dict:
        return {"dataset": self.settings.dataset, "snapshot": self.dataset.snapshot, "model": self.settings.embedding_model,
                "dimensions": self.settings.embedding_dimensions, "profile": self.settings.embedding_profile}

    @property
    def driver(self):
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
            except ImportError:
                raise BackendUnavailable("缺少 Neo4j Driver，请安装项目 requirements.txt。") from None
            self._driver = GraphDatabase.driver(self.settings.neo4j_uri,
                                               auth=(self.settings.neo4j_user, self.settings.neo4j_password.get_secret_value()),
                                               connection_timeout=5, max_transaction_retry_time=3)
        return self._driver

    def _read(self, query: str, **params) -> list[dict]:
        driver = self.driver
        from neo4j import READ_ACCESS
        from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

        try:
            with driver.session(database=self.settings.neo4j_database, default_access_mode=READ_ACCESS) as session:
                return session.execute_read(lambda tx: [dict(row) for row in tx.run(query, **{**self.parameters, **params})])
        except (Neo4jError, ServiceUnavailable, SessionExpired) as exc:
            raise BackendUnavailable("Neo4j 连接或查询失败，请核对实际数据库名、凭据和权限。") from exc

    def _chunks(self, rows: list[dict]) -> list[KnowledgeChunk]:
        result = []
        for record in rows:
            row = record["chunk"]
            expected = self.chunk_map.get(row.get("knowledge_id"))
            if expected is None or any(row.get(k) != expected[k] for k in ("title", "text", "entity_ids", "source_ids", "fact_scope", "category", "text_type")):
                raise DataVersionError("Neo4j 知识正文或适用范围与启用的数据快照不一致。")
            result.append(chunk_from_record(row, record.get("score")))
        return result

    def related(self, entity_ids: list[str], limit: int = 3) -> list[KnowledgeChunk]:
        if not entity_ids:
            return []
        rows = self._read("""
            MATCH (k:QAEntity:Chunk)-[r:ABOUT]->(e:QAEntity)
            WHERE """ + CHUNK_SCOPE + """
              AND e.entity_id IN $entity_ids
              AND e.qa_dataset = $dataset AND e.qa_snapshot = $snapshot
              AND r.qa_dataset = $dataset AND r.qa_snapshot = $snapshot
            RETURN DISTINCT properties(k) AS chunk ORDER BY chunk.knowledge_id LIMIT $limit
            """, entity_ids=entity_ids, limit=limit)
        return self._chunks(rows)

    def ensure_vector_ready(self) -> None:
        if not self.settings.embedding_model:
            raise KnowledgeNotReady("Embedding 模型未配置，知识语义问答尚未就绪。")
        indexes = self._read("SHOW VECTOR INDEXES YIELD name, state, labelsOrTypes, properties, options WHERE name = $name RETURN state, labelsOrTypes, properties, options",
                             name=self.settings.neo4j_vector_index)
        if len(indexes) != 1:
            raise KnowledgeNotReady("未找到配置的 Neo4j 向量索引，请先运行离线向量构建脚本。")
        validate_index(indexes[0], self.settings)
        rows = self._read("MATCH (k:QAEntity:Chunk) WHERE " + CHUNK_SCOPE + " RETURN properties(k) AS chunk")
        if {r["chunk"].get("knowledge_id") for r in rows} != set(self.chunk_map) or len(rows) != len(self.chunk_map):
            raise DataVersionError("Neo4j 当前快照的知识实体集合与正式数据不一致。")
        self._chunks(rows)
        for r in rows:
            k = r["chunk"]
            if (k.get("embedding_model") != self.settings.embedding_model or k.get("embedding_profile") != self.settings.embedding_profile
                    or k.get("embedding_dimensions") != self.settings.embedding_dimensions or k.get("embedding_text_hash") != k.get("text_hash")):
                raise KnowledgeNotReady("部分知识向量缺失、正文已变更或模型版本不匹配，请重建向量。")
            try:
                validate_vector(k.get("embedding", []), self.settings.embedding_dimensions)
            except (TypeError, ValueError, ModelError) as exc:
                raise KnowledgeNotReady("知识向量格式不正确，请重建向量。") from exc

    def ensure_graph_ready(self) -> None:
        nodes = self._read("MATCH (n:QAEntity) WHERE n.qa_dataset = $dataset AND n.qa_snapshot = $snapshot RETURN n.entity_id AS entity_id, n.entity_type AS entity_type")
        if len(nodes) != len(self.dataset.node_types) or {r["entity_id"]: r["entity_type"] for r in nodes} != self.dataset.node_types:
            raise DataVersionError("Neo4j 当前归属及快照的实体集合与正式数据不一致。")
        relations = self._read("""
            MATCH (a:QAEntity)-[r]->(b:QAEntity)
            WHERE r.qa_dataset = $dataset AND r.qa_snapshot = $snapshot
              AND a.qa_dataset = $dataset AND a.qa_snapshot = $snapshot
              AND b.qa_dataset = $dataset AND b.qa_snapshot = $snapshot
            RETURN r.relation_id AS relation_id, type(r) AS relation_type, a.entity_id AS head_id, b.entity_id AS tail_id
            """)
        expected = {r["relation_id"]: (r["relation_type"], r["head_id"], r["tail_id"]) for r in self.dataset.tables["relations"]}
        actual = {r["relation_id"]: (r["relation_type"], r["head_id"], r["tail_id"]) for r in relations}
        if actual != expected or len(relations) != len(expected):
            raise DataVersionError("Neo4j 当前快照的关系集合与正式数据不一致。")

    def search(self, vector: list[float], entity_ids: list[str], category: str | None, limit: int) -> list[KnowledgeChunk]:
        vector = validate_vector(vector, self.settings.embedding_dimensions)
        # 先限定合法候选再精确计算余弦，兼容 Neo4j 5.26，避免全局 ANN 后过滤丢失合格片段。
        # 当前仅 138 个片段；大规模时可用支持预过滤的 ANN 实现替换此查询。
        rows = self._read("""
            MATCH (k:QAEntity:Chunk)
            WHERE """ + CHUNK_SCOPE + " AND " + VECTOR_SCOPE + """
              AND ($category IS NULL OR k.category = $category)
              AND (size($entity_ids) = 0 OR any(e IN k.entity_ids WHERE e IN $entity_ids))
            WITH k, vector.similarity.cosine(k.embedding, $vector) AS score
            WHERE score IS NOT NULL
            RETURN properties(k) AS chunk, score ORDER BY score DESC, k.knowledge_id LIMIT $limit
            """, vector=vector, entity_ids=entity_ids, category=category, limit=limit)
        return self._chunks(rows)

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None
