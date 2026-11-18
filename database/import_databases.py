"""使用项目配置同步MySQL与Neo4j；默认只显示计划，--apply执行并保存备份。"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.import_neo4j import apply_snapshot, build_plan, ensure_schema
from qa.core.config import Settings
from qa.core.paths import ROOT
from qa.db.mysql import connect_mysql, decode_row
from qa.db.mysql_tables import build_tables
from qa.db.neo4j import connect_neo4j
from qa.db.snapshot import json_text, load_dataset
from qa.repository.mysql import MySQLRepository
from qa.repository.neo4j import Neo4jRepository


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def verify_mysql(connection, tables) -> dict:
    counts = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"SELECT {','.join('`'+c+'`' for c in table.columns)} FROM `{table.name}`")
            rows = [decode_row(r) for r in cursor.fetchall()]
            key = sorted(table.primary)
            actual = {tuple(r[c] for c in key): r for r in rows}
            expected = {tuple(r[c] for c in key): r for r in table.rows}
            if len(actual) != len(rows) or actual != expected:
                raise RuntimeError(f"MySQL {table.name} 的真实字段或记录集合与正式JSONL不一致")
            counts[table.name] = len(rows)
    return counts


def upsert_mysql(connection, tables, batch_size=50) -> None:
    with connection.cursor() as cursor:
        for table in tables:
            columns = ",".join("`" + c + "`" for c in table.columns)
            updates = ",".join(f"`{c}`=incoming.`{c}`" for c in table.columns if c not in table.primary)
            for start in range(0, len(table.rows), batch_size):
                batch = table.rows[start:start + batch_size]
                values = ",".join("(" + ",".join(["%s"] * len(table.columns)) + ")" for _ in batch)
                parameters = [json_text(r[c]) if c in table.json_columns and r[c] is not None else r[c]
                              for r in batch for c in table.columns]
                cursor.execute(f"INSERT INTO `{table.name}` ({columns}) VALUES {values} AS incoming ON DUPLICATE KEY UPDATE {updates}", parameters)


def import_all(settings: Settings, base_dir: Path | None, backup_dir: Path, build_vectors: bool) -> dict:
    dataset = load_dataset(settings.data_dir)
    tables, plan = build_tables(dataset), build_plan(dataset)
    baseline = load_dataset(base_dir) if base_dir else dataset
    backup_dir = backup_dir.resolve()
    if not backup_dir.is_relative_to((ROOT / "work").resolve()):
        raise ValueError("数据库备份目录须位于项目work内")
    if (backup_dir / "mysql_before.json").exists() or (backup_dir / "neo4j_before.json").exists():
        raise ValueError("备份已存在，请保留原备份并选择新的备份目录")
    MySQLRepository(settings, baseline).ensure_ready()
    graph = Neo4jRepository(settings, baseline)
    try:
        graph.ensure_graph_ready()
    finally:
        graph.close()
    print("两套数据库的导入前版本核对通过。", flush=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ["neo4j_import_report.json", "mysql_import_report.json", "embedding_build_report.json", "database_import_report.json"]:
        source = Path(__file__).parent / name
        if source.is_file():
            (backup_dir / name).write_bytes(source.read_bytes())
    mysql_before = {}
    with closing(connect_mysql(settings, io_timeout=30)) as connection:
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT * FROM `{table.name}`")
                mysql_before[table.name] = [decode_row(r) for r in cursor.fetchall()]
        save(backup_dir / "mysql_before.json", {"snapshot": baseline.snapshot, "database": settings.mysql_database, "tables": mysql_before})
        with connect_neo4j(settings) as driver:
            with driver.session(database=settings.neo4j_database) as session:
                nodes = [dict(r) for r in session.run("MATCH (n:QAEntity) WHERE n.qa_dataset=$dataset RETURN labels(n) AS labels, properties(n) AS properties", dataset=settings.dataset)]
                relations = [dict(r) for r in session.run("MATCH (a:QAEntity)-[r]->(b:QAEntity) WHERE r.qa_dataset=$dataset RETURN a.entity_id AS head_id,b.entity_id AS tail_id,type(r) AS relation_type,properties(r) AS properties", dataset=settings.dataset)]
                save(backup_dir / "neo4j_before.json", {"snapshot": baseline.snapshot, "database": settings.neo4j_database,
                                                       "nodes": nodes, "relationships": relations})
                print("已保存数据库原记录及现有向量备份。", flush=True)
                try:
                    upsert_mysql(connection, tables)
                    counts = verify_mysql(connection, tables)
                    print("MySQL写入及事务内逐字段检查通过。", flush=True)
                    ensure_schema(session)
                    graph_result = session.execute_write(apply_snapshot, plan, 200)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
    report_dir = Path(__file__).parent
    mysql_report = {"mode": "imported", "database": settings.mysql_database, "snapshot": dataset.snapshot,
                    "verified_table_counts": counts, "transaction_committed": True}
    graph_report = {"mode": "imported", "database": settings.neo4j_database, **graph_result}
    save(report_dir / "mysql_import_report.json", mysql_report)
    save(report_dir / "neo4j_import_report.json", graph_report)
    MySQLRepository(settings, dataset).ensure_ready()
    graph = Neo4jRepository(settings, dataset)
    try:
        graph.ensure_graph_ready()
    finally:
        graph.close()
    print("MySQL与Neo4j导入后版本和记录集合核对通过。", flush=True)
    result = {"mode": "imported", "completed_at": datetime.now(timezone.utc).isoformat(),
              "snapshot": dataset.snapshot, "backup_dir": str(backup_dir), "mysql": mysql_report, "neo4j": graph_report}
    save(report_dir / "database_import_report.json", result)
    if build_vectors:
        from scripts.build_embeddings import build
        vectors = build(settings, batch_size=16)
        save(report_dir / "embedding_build_report.json", vectors)
        result["vectors"] = vectors
        save(report_dir / "database_import_report.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--build-vectors", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.from_env().model_copy(update={"data_dir": args.data_dir.resolve()})
        dataset = load_dataset(settings.data_dir)
        if args.apply:
            if args.backup_dir is None:
                parser.error("实际导入须指定--backup-dir，备份放在项目work目录")
            result = import_all(settings, args.base_dir, args.backup_dir, args.build_vectors)
        else:
            result = {"mode": "dry_run", "connected_database": False, "snapshot": dataset.snapshot,
                      "mysql_table_counts": {t.name: len(t.rows) for t in build_tables(dataset)},
                      "neo4j": build_plan(dataset)["summary"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # 数据库/模型异常可能携带连接地址；终端只显示异常类型及本程序的检查原因。
        report = {"mode": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, (RuntimeError, ValueError)):
            report["reason"] = str(exc)
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
