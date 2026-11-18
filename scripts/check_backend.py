"""按需诊断入口；默认仅报告本地数据与配置，--online 才连接数据库。"""

import argparse
import json
import sys

from qa.config import Settings
from qa.data import runtime_dataset
from qa.errors import ConfigurationError, KnowledgeNotReady, QAError
from qa.mysql_repository import MySQLRepository
from qa.neo4j_repository import Neo4jRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="只读检查 MySQL 真实字段及 Neo4j 归属/快照")
    parser.add_argument("--require-vectors", action="store_true", help="要求语义检索就绪；需同时使用 --online")
    args = parser.parse_args()
    if args.require_vectors and not args.online:
        parser.error("--require-vectors 需要 --online")
    graph = None
    try:
        settings = Settings.from_env()
        dataset = runtime_dataset(settings)
        report = {"backend": settings.backend, "snapshot": dataset.snapshot, "counts": {k: len(v) for k, v in dataset.tables.items()},
                  "connected_database": False, "model_called": False, "llm_configured": bool(settings.llm_model),
                  "embedding_configured": bool(settings.embedding_model), "price_enabled": settings.price_enabled,
                  "display_price_enabled": settings.display_price_enabled}
        if args.online:
            if settings.backend != "database":
                raise ConfigurationError("在线诊断需要显式设置 QA_BACKEND=database。")
            MySQLRepository(settings, dataset).ensure_ready()
            graph = Neo4jRepository(settings, dataset)
            graph.ensure_graph_ready()
            report.update(connected_database=True, mysql="ready", neo4j="ready", neo4j_database=settings.neo4j_database)
            try:
                graph.ensure_vector_ready()
                report["knowledge"] = "ready"
            except KnowledgeNotReady as exc:
                report["knowledge"] = str(exc)
                if args.require_vectors:
                    raise
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except QAError as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if graph:
            graph.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
