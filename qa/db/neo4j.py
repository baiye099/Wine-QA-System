"""Neo4j 驱动、只读事务与向量索引元数据校验。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.errors import BackendUnavailable, KnowledgeNotReady

if TYPE_CHECKING:
    from ..core.config import Settings


def connect_neo4j(settings: Settings):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise BackendUnavailable("缺少 Neo4j Driver，请安装项目 requirements.txt。") from None
    return GraphDatabase.driver(settings.neo4j_uri,
                                auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
                                connection_timeout=5, max_transaction_retry_time=3)


def read_cypher(driver, database: str, query: str, parameters: dict) -> list[dict]:
    from neo4j import READ_ACCESS
    from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

    try:
        with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            return session.execute_read(lambda tx: [dict(row) for row in tx.run(query, **parameters)])
    except (Neo4jError, ServiceUnavailable, SessionExpired) as exc:
        raise BackendUnavailable("Neo4j 连接或查询失败，请核对实际数据库名、凭据和权限。") from exc


def validate_index(row: dict, settings: Settings, require_online: bool = True) -> None:
    config = row.get("options", {}).get("indexConfig", {})
    # 不同版本可能返回 COSINE 或 cosine。
    similarity = config.get("vector.similarity_function")
    cosine = isinstance(similarity, str) and similarity.casefold() == "cosine"
    if (row.get("labelsOrTypes") != ["Chunk"] or row.get("properties") != ["embedding"]
            or config.get("vector.dimensions") != settings.embedding_dimensions or not cosine):
        raise KnowledgeNotReady("同名向量索引的标签、属性、维度或相似度与配置不一致，请离线处理。")
    if require_online and row.get("state") != "ONLINE":
        raise KnowledgeNotReady("Neo4j 向量索引尚未 ONLINE，请等待离线构建完成。")
