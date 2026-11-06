"""导入本项目正式JSONL到Neo4j；--dry-run只读本地文件，不连接数据库。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dataset import ROOT, RELATION_TYPES, Dataset, json_text, load_dataset, normalized_alias


DATASET_NAME = "wine-qa-system"
# 这些是本项目曾经使用的数据归属属性值，不是要连接的Neo4j数据库名。
LEGACY_DATASET_NAMES = ("Wine_QA_System", "ningxia_wine_qa")
NODE_LABELS = {"Document", "Winery", "Product", "Region", "GrapeVariety", "Flavor", "Concept", "Chunk"}
SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT qa_entity_id_unique IF NOT EXISTS FOR (n:QAEntity) REQUIRE n.entity_id IS UNIQUE",
    "CREATE INDEX qa_winery_name IF NOT EXISTS FOR (n:Winery) ON (n.name)",
    "CREATE INDEX qa_product_name IF NOT EXISTS FOR (n:Product) ON (n.name)",
    "CREATE INDEX qa_chunk_category IF NOT EXISTS FOR (n:Chunk) ON (n.category)",
]


def property_map(record: dict) -> dict:
    """Neo4j属性不能保存嵌套map；复杂结构存为明确命名的JSON字符串。"""
    result = {}
    scalar = (str, bool, int, float)
    for key, value in record.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Neo4j属性不能是非有限值: {key}")
        native_list = isinstance(value, list) and (not value or all(type(v) is type(value[0]) and isinstance(v, scalar) for v in value))
        if isinstance(value, dict) or (isinstance(value, list) and not native_list):
            result[key + "_json"] = json_text(value)
        elif value is None or isinstance(value, scalar) or native_list:
            result[key] = value
        else:
            raise TypeError(f"不支持的Neo4j属性类型: {key}: {type(value).__name__}")
    result["raw_record_json"] = json_text(record)
    return result


def build_plan(ds: Dataset) -> dict:
    aliases, alias_keys = defaultdict(set), defaultdict(set)
    for a in ds.tables["aliases"]:
        aliases[a["entity_id"]].add(a["alias"])
        alias_keys[a["entity_id"]].add(normalized_alias(a["alias"]))
    nodes = []
    for table, id_key, fixed_label in [("sources", "source_id", "Document"), ("wineries", "winery_id", "Winery"),
                                       ("products", "product_id", "Product"), ("entities", "entity_id", None), ("knowledge", "knowledge_id", "Chunk")]:
        for record in ds.tables[table]:
            eid, label = record[id_key], fixed_label or record["entity_type"]
            if label not in NODE_LABELS:
                raise ValueError(f"未允许的标签: {label}")
            props = property_map(record)
            props.update({"entity_id": eid, "entity_type": label, "qa_dataset": DATASET_NAME,
                          "qa_snapshot": ds.snapshot, "aliases": sorted(aliases[eid]), "alias_keys": sorted(alias_keys[eid])})
            if label == "Product":
                for field, flag in record["field_filter_eligibility"].items():
                    props["filter_" + field] = flag
            if label == "Chunk":
                props["text_hash"] = hashlib.sha256(record["text"].encode("utf-8")).hexdigest()
            nodes.append({"entity_id": eid, "label": label, "properties": props})
    rels = []
    for record in ds.tables["relations"]:
        props = property_map(record)
        props.update({"qa_dataset": DATASET_NAME, "qa_snapshot": ds.snapshot})
        rels.append({"relation_id": record["relation_id"], "head_id": record["head_id"], "tail_id": record["tail_id"],
                     "relation_type": record["relation_type"], "properties": props})
    return {"snapshot": ds.snapshot, "nodes": sorted(nodes, key=lambda r: r["entity_id"]),
            "relations": sorted(rels, key=lambda r: r["relation_id"]),
            "summary": {"dataset_name": DATASET_NAME, "node_count": len(nodes), "nodes_by_label": dict(sorted(Counter(n["label"] for n in nodes).items())),
                        "relationship_count": len(rels), "relationships_by_type": dict(sorted(Counter(r["relation_type"] for r in rels).items())),
                        "alias_count_as_properties": len(ds.tables["aliases"]), "snapshot": ds.snapshot}}


def batches(rows: list, batch_size: int):
    for start in range(0, len(rows), batch_size):
        yield rows[start:start + batch_size]


def node_query(label: str) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"禁止的节点标签: {label}")
    invalidate = ""
    if label == "Chunk":
        invalidate = """
        FOREACH (ignored IN CASE WHEN n.text_hash = row.properties.text_hash THEN [] ELSE [1] END |
          REMOVE n.embedding, n.embedding_model, n.embedding_dimensions, n.embedding_updated_at)
        """
    return f"""
    UNWIND $rows AS row
    MERGE (n:QAEntity {{entity_id: row.entity_id}})
    SET n:{label}
    {invalidate}
    SET n += row.properties
    RETURN count(n) AS imported
    """


def relationship_query(kind: str) -> str:
    if kind not in RELATION_TYPES:
        raise ValueError(f"禁止的关系类型: {kind}")
    return f"""
    UNWIND $rows AS row
    MATCH (a:QAEntity {{entity_id: row.head_id}}), (b:QAEntity {{entity_id: row.tail_id}})
    MERGE (a)-[r:{kind} {{relation_id: row.relation_id}}]->(b)
    SET r += row.properties
    RETURN count(r) AS imported
    """


def legacy_owner(record: dict, snapshot: str) -> str | None:
    """仅接受已知旧名且可证明对应同一数据的记录，不认领无标记或其他项目数据。"""
    owner = record.get("dataset_name")
    if owner == DATASET_NAME:
        return None
    if owner not in LEGACY_DATASET_NAMES:
        raise ValueError(f"已有归属={owner!r}，不是本项目当前或已知旧标识")
    if record.get("snapshot") != snapshot and record.get("same_record") is not True:
        raise ValueError(f"已有归属={owner!r}，但数据快照和规范记录均不匹配，不能自动迁移")
    return owner


def check_existing_ownership(tx, plan: dict) -> dict:
    """先检查所有既有记录；更新旧标识由后面的正常upsert在同一事务内完成。"""
    expected_nodes = {n["entity_id"]: n for n in plan["nodes"]}
    existing_nodes = list(tx.run("""
        UNWIND $rows AS row
        MATCH (n:QAEntity {entity_id: row.entity_id})
        RETURN n.entity_id AS entity_id, n.qa_dataset AS dataset_name,
               n.entity_type AS entity_type, labels(n) AS node_labels,
               n.qa_snapshot AS snapshot,
               coalesce(n.raw_record_json, '') = row.raw_record_json AS same_record
        """, rows=[{"entity_id": n["entity_id"], "raw_record_json": n["properties"]["raw_record_json"]} for n in plan["nodes"]]))
    node_migrations, relation_migrations, conflicts = Counter(), Counter(), []
    seen_nodes = set()
    for record in existing_nodes:
        eid = record["entity_id"]
        expected = expected_nodes[eid]
        try:
            if eid in seen_nodes:
                raise ValueError("同一业务ID存在多个QAEntity节点")
            seen_nodes.add(eid)
            if record.get("entity_type") != expected["label"] or expected["label"] not in (record.get("node_labels") or []):
                raise ValueError(f"已有类型={record.get('entity_type')!r}、标签={record.get('node_labels')!r}，预期类型={expected['label']!r}")
            previous_owner = legacy_owner(record, plan["snapshot"])
            if previous_owner:
                node_migrations[previous_owner] += 1
        except ValueError as exc:
            conflicts.append(f"{eid}: {exc}")
    if conflicts:
        raise ValueError("业务ID归属/类型检查未通过：" + "; ".join(conflicts[:10]))

    expected_relations = {r["relation_id"]: r for r in plan["relations"]}
    existing_relations = list(tx.run("""
        UNWIND $rows AS row
        MATCH (a:QAEntity)-[r {relation_id: row.relation_id}]->(b:QAEntity)
        RETURN r.relation_id AS relation_id, r.qa_dataset AS dataset_name,
               type(r) AS relation_type, a.entity_id AS head_id, b.entity_id AS tail_id,
               r.qa_snapshot AS snapshot,
               coalesce(r.raw_record_json, '') = row.raw_record_json AS same_record
        """, rows=[{"relation_id": r["relation_id"], "raw_record_json": r["properties"]["raw_record_json"]} for r in plan["relations"]]))
    seen_relations = set()
    for record in existing_relations:
        rid = record["relation_id"]
        expected = expected_relations[rid]
        try:
            if rid in seen_relations:
                raise ValueError("同一关系ID存在多条关系")
            seen_relations.add(rid)
            if any(record.get(field) != expected[field] for field in ["relation_type", "head_id", "tail_id"]):
                raise ValueError("已有关系类型或端点与待导入记录不同")
            previous_owner = legacy_owner(record, plan["snapshot"])
            if previous_owner:
                relation_migrations[previous_owner] += 1
        except ValueError as exc:
            conflicts.append(f"{rid}: {exc}")
    if conflicts:
        raise ValueError("关系ID归属/端点检查未通过：" + "; ".join(conflicts[:10]))
    return {"nodes_by_previous_dataset": dict(sorted(node_migrations.items())),
            "relationships_by_previous_dataset": dict(sorted(relation_migrations.items()))}


def apply_snapshot(tx, plan: dict, batch_size: int) -> dict:
    ownership_migration = check_existing_ownership(tx, plan)
    node_groups, rel_groups = defaultdict(list), defaultdict(list)
    for n in plan["nodes"]:
        node_groups[n["label"]].append(n)
    for r in plan["relations"]:
        rel_groups[r["relation_type"]].append(r)
    for label, records in sorted(node_groups.items()):
        for batch in batches(records, batch_size):
            count = tx.run(node_query(label), rows=batch).single()["imported"]
            if count != len(batch):
                raise RuntimeError(f"节点数量不匹配: {label}: {count}/{len(batch)}")
    for kind, records in sorted(rel_groups.items()):
        for batch in batches(records, batch_size):
            count = tx.run(relationship_query(kind), rows=batch).single()["imported"]
            if count != len(batch):
                raise RuntimeError(f"关系端点缺失或数量不匹配: {kind}: {count}/{len(batch)}")
    node_count = tx.run("""
        MATCH (n:QAEntity {qa_dataset: $dataset, qa_snapshot: $snapshot})
        RETURN count(n) AS total
        """, dataset=DATASET_NAME, snapshot=plan["snapshot"]).single()["total"]
    rel_count = tx.run("""
        MATCH (:QAEntity)-[r]->(:QAEntity)
        WHERE r.qa_dataset = $dataset AND r.qa_snapshot = $snapshot
        RETURN count(r) AS total
        """, dataset=DATASET_NAME, snapshot=plan["snapshot"]).single()["total"]
    if node_count != len(plan["nodes"]) or rel_count != len(plan["relations"]):
        raise RuntimeError("事务内总量校验失败，将回滚本次节点和关系写入")
    return {**plan["summary"], "verified_node_count": node_count, "verified_relationship_count": rel_count,
            "ownership_migration": ownership_migration}


def ensure_schema(session) -> None:
    for statement in SCHEMA_STATEMENTS:
        session.run(statement).consume()
    constraints = list(session.run("SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties WHERE name = 'qa_entity_id_unique' RETURN type, labelsOrTypes, properties"))
    if (len(constraints) != 1 or constraints[0]["labelsOrTypes"] != ["QAEntity"] or constraints[0]["properties"] != ["entity_id"]
            or constraints[0]["type"] not in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}):
        raise RuntimeError("同名约束未作用于QAEntity.entity_id，请检查现有数据库结构")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "wine-qa-system"), help="实际Neo4j数据库名，默认wine-qa-system；可用NEO4J_DATABASE覆盖")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true", help="仅校验本地数据并显示计划，不导入也不连接数据库")
    parser.add_argument("--report-file", type=Path, help="实际导入成功后将结果另存JSON；不写入data目录")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("batch-size必须大于0")
    report_path = args.report_file.resolve() if args.report_file else None
    if report_path:
        for protected in [args.data_dir.resolve(), (ROOT / "data_raw").resolve()]:
            if report_path == protected or protected in report_path.parents:
                parser.error("导入报告不能写入data或data_raw，请使用database目录")
    ds = load_dataset(args.data_dir)
    plan = build_plan(ds)
    if args.dry_run:
        print(json.dumps({"mode": "dry_run", "connected_database": False, "database": args.database, **plan["summary"]}, ensure_ascii=False, indent=2))
        return
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise SystemExit("请先执行 python -m pip install -r database/requirements.txt") from exc
    password = os.getenv("NEO4J_PASSWORD") or getpass.getpass("Neo4j密码（输入不显示）: ")
    if not password:
        raise SystemExit("密码不能为空")
    with GraphDatabase.driver(args.uri, auth=(args.user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=args.database) as session:
            # Neo4j的Schema操作使用独立事务；节点和关系写入归入下方一个受管理事务。
            ensure_schema(session)
            result = session.execute_write(apply_snapshot, plan, args.batch_size)
    report = {"mode": "imported", "database": args.database, **result}
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
