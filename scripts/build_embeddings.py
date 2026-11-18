"""离线写入 Chunk 向量并创建索引；--dry-run 不联网、不连接数据库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

from qa.clients.http import HTTPEmbeddingModel
from qa.core.config import Settings
from qa.core.errors import BackendUnavailable, ConfigurationError, DataVersionError, ModelError, QAError
from qa.db.neo4j import validate_index
from qa.db.snapshot import runtime_dataset
from qa.models.vector import validate_vector
from qa.repository.neo4j import Neo4jRepository


def write_batch(tx, rows: list[dict], parameters: dict) -> None:
    result = tx.run("""
        UNWIND $rows AS row
        MATCH (k:QAEntity:Chunk {knowledge_id: row.knowledge_id})
        WHERE k.qa_dataset = $dataset AND k.qa_snapshot = $snapshot
          AND k.retrieval_allowed = true AND k.review_status = 'accepted'
          AND k.contains_price = false AND k.text_hash = row.text_hash AND k.text = row.text
        SET k.embedding = row.embedding, k.embedding_model = $model,
            k.embedding_dimensions = $dimensions, k.embedding_profile = $profile,
            k.embedding_text_hash = row.text_hash, k.embedding_updated_at = $updated_at
        RETURN count(k) AS written
        """, rows=rows, **parameters).single()
    if result is None or result["written"] != len(rows):
        raise DataVersionError("写向量时正文或数据快照发生变化，已回滚当前批次。")


def build(settings: Settings, batch_size: int = 16, force: bool = False) -> dict:
    if settings.backend != "database":
        raise ConfigurationError("实际构建向量需要显式设置 QA_BACKEND=database；仅查看计划请加 --dry-run。")
    if not settings.embedding_model:
        raise ConfigurationError("请先配置独立的 EMBEDDING_BASE_URL、MODEL、DIMENSIONS 和鉴权。")
    dataset = runtime_dataset(settings)
    repository = Neo4jRepository(settings, dataset)
    driver = repository.driver
    try:
        from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

        repository.ensure_graph_ready()
        indexes = repository.vector_indexes()
        if indexes:
            validate_index(indexes[0], settings, require_online=False)
        records = repository.knowledge_records()
        pending = []
        for row in records:
            text_hash = hashlib.sha256(row["text"].encode()).hexdigest()
            if text_hash != row.get("text_hash"):
                raise DataVersionError("知识文本哈希不匹配，请先按正式快照重新导入图数据。")
            current = (row.get("embedding_model") == settings.embedding_model and row.get("embedding_profile") == settings.embedding_profile
                       and row.get("embedding_dimensions") == settings.embedding_dimensions and row.get("embedding_text_hash") == text_hash)
            if current:
                try:
                    validate_vector(row.get("embedding", []), settings.embedding_dimensions)
                except ModelError:
                    current = False
            if force or not current:
                pending.append(row)
        model = HTTPEmbeddingModel(settings)
        updated_at = datetime.now(timezone.utc).isoformat()
        with driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(pending), batch_size):
                batch = pending[start:start + batch_size]
                vectors = model.embed([r["text"] for r in batch])
                rows = [{"knowledge_id": r["knowledge_id"], "text": r["text"], "text_hash": r["text_hash"], "embedding": v}
                        for r, v in zip(batch, vectors, strict=True)]
                session.execute_write(write_batch, rows, {**repository.parameters, "updated_at": updated_at})
                print(f"已写入 {min(start + batch_size, len(pending))}/{len(pending)} 个向量。", file=sys.stderr)
            # 索引名经过 Settings 标识符白名单校验，维度作为参数传入。
            session.run(f"""
                CREATE VECTOR INDEX `{settings.neo4j_vector_index}` IF NOT EXISTS
                FOR (k:Chunk) ON (k.embedding)
                OPTIONS {{indexConfig: {{`vector.dimensions`: $dimensions, `vector.similarity_function`: 'cosine'}}}}
                """, dimensions=settings.embedding_dimensions).consume()
            session.run("CALL db.awaitIndex($name, 30)", name=settings.neo4j_vector_index).consume()
        repository.ensure_vector_ready()
        return {"mode": "built", "snapshot": dataset.snapshot, "database": settings.neo4j_database,
                "index": settings.neo4j_vector_index, "updated": len(pending), "skipped": len(records) - len(pending),
                "model": settings.embedding_model, "dimensions": settings.embedding_dimensions, "ready": True}
    except (Neo4jError, ServiceUnavailable, SessionExpired) as exc:
        raise BackendUnavailable("离线向量写入或索引操作失败，请检查数据库版本、权限和索引配置。") from exc
    finally:
        repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="重算当前快照的全部合格片段")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 128:
        parser.error("batch-size 必须在 1–128 之间")
    try:
        settings = Settings.from_env()
        if args.dry_run:
            dataset = runtime_dataset(settings)
            report = {"mode": "dry_run", "connected_database": False, "called_model": False, "snapshot": dataset.snapshot,
                      "eligible_chunks": len(dataset.tables["knowledge"]), "model": settings.embedding_model or "未配置",
                      "dimensions": settings.embedding_dimensions, "index": settings.neo4j_vector_index}
        else:
            report = build(settings, args.batch_size, args.force)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except QAError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

