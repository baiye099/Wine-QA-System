"""读取并核对运行时与离线工具共用的正式数据快照。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import DataVersionError
from ..models.dataset import Dataset

if TYPE_CHECKING:
    from ..core.config import Settings


ID_FIELDS = {
    "sources": "source_id", "wineries": "winery_id", "products": "product_id",
    "price_observations": "price_id", "entities": "entity_id", "aliases": "alias_id",
    "knowledge": "knowledge_id", "relations": "relation_id", "professional_reviews": "review_id",
}
ENTITY_TYPES = {"Region", "GrapeVariety", "Flavor", "Concept"}
RELATION_TYPES = {
    "PRODUCES": ({"Winery"}, {"Product"}),
    "LOCATED_IN": ({"Winery"}, {"Region"}),
    "ORIGINATES_FROM": ({"Product"}, {"Region"}),
    "MADE_FROM": ({"Product"}, {"GrapeVariety"}),
    "HAS_FLAVOR": ({"Product"}, {"Flavor"}),
    "PART_OF": ({"Region"}, {"Region"}),
    "HAS_CHUNK": ({"Document"}, {"Chunk"}),
    "ABOUT": ({"Chunk"}, {"Winery", "Product", *ENTITY_TYPES}),
}


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict]:
    result = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"非法JSON数值: {x}")))
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: 每行必须是JSON对象")
            if row.get("review_status") != "accepted":
                raise ValueError(f"{path}:{line_number}: 只允许accepted记录，不读取review/excluded/auxiliary")
            result.append(row)
    return result


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def load_dataset(data_dir: Path) -> Dataset:
    data_dir = data_dir.resolve()
    tables, hashes = {}, {}
    manifest_path = data_dir / "manifest.json"
    expected = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {r["path"]: r["sha256"] for r in manifest.get("output_files", [])}
    for name, id_key in ID_FIELDS.items():
        path = data_dir / (name + ".jsonl")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name in expected:
            require(digest == expected[path.name], f"{path.name}已偏离清洗manifest，请重新清洗或复核数据版本")
        hashes[path.name] = digest
        records = read_jsonl(path)
        ids = []
        for r in records:
            value = r.get(id_key)
            require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", value) is not None, f"{name}: 非法业务ID {value!r}")
            ids.append(value)
        require(len(set(ids)) == len(ids), f"{name}: ID重复")
        tables[name] = sorted(records, key=lambda r: r[id_key])
    source_ids = {s["source_id"] for s in tables["sources"]}
    require(all(s.get("dataset_role") == "real" for s in tables["sources"]), "正式来源中包含演示数据")
    node_types = {}
    for table, id_key, kind in [("sources", "source_id", "Document"), ("wineries", "winery_id", "Winery"),
                               ("products", "product_id", "Product"), ("entities", "entity_id", None), ("knowledge", "knowledge_id", "Chunk")]:
        for record in tables[table]:
            eid = record[id_key]
            require(eid not in node_types, f"跨类型ID冲突: {eid}")
            actual_kind = kind or record["entity_type"]
            require(kind is not None or actual_kind in ENTITY_TYPES, f"未允许的实体类型: {actual_kind}")
            node_types[eid] = actual_kind
    for name, records in tables.items():
        if name == "sources":
            continue
        for record in records:
            sources = record.get("source_ids")
            require(isinstance(sources, list) and sources and all(s in source_ids for s in sources), f"{name}: 来源引用缺失")
    for record in tables["wineries"]:
        if record.get("region_id"):
            require(node_types.get(record["region_id"]) == "Region", f"酒庄产区错误: {record['winery_id']}")
    for record in tables["products"]:
        pid = record["product_id"]
        require(node_types.get(record["winery_id"]) == "Winery", f"产品酒庄缺失: {pid}")
        if record.get("region_id"):
            require(node_types.get(record["region_id"]) == "Region", f"产品产区错误: {pid}")
        require((record.get("vintage") is not None) == (record["vintage_status"] == "specified"), f"年份状态错误: {pid}")
        require(record["record_scope"] != "catalog_candidate", f"目录候选进入正式数据: {pid}")
        for field, allowed in record["field_filter_eligibility"].items():
            require(isinstance(allowed, bool), f"筛选资格必须为布尔: {pid}/{field}")
            if allowed:
                require(record.get(field) is not None and record.get(field) != [], f"空字段被允许筛选: {pid}/{field}")
    for table in ["price_observations", "professional_reviews"]:
        for record in tables[table]:
            require(node_types.get(record["product_id"]) == "Product", f"{table}: 产品引用缺失")
            require(record.get("source_id") in source_ids, f"{table}: 单来源引用缺失")
    for price in tables["price_observations"]:
        require(0 < price["amount_min"] <= price["amount_max"], "报价金额错误")
    for alias in tables["aliases"]:
        require(node_types.get(alias["entity_id"]) == alias["entity_type"], "别名目标或类型错误")
        require(alias["entity_type"] not in {"Chunk", "Document"}, "别名目标类型不在实体目录")
    for chunk in tables["knowledge"]:
        require(chunk.get("retrieval_allowed") is True and chunk.get("contains_price") is False, "知识检索或价格隔离标志错误")
        require(bool(chunk.get("text")) and all(e in node_types for e in chunk["entity_ids"]), "知识正文或对象引用错误")
    for rel in tables["relations"]:
        kind = rel["relation_type"]
        require(kind in RELATION_TYPES, f"未允许的关系类型: {kind}")
        heads, tails = RELATION_TYPES[kind]
        require(node_types.get(rel["head_id"]) in heads and node_types.get(rel["tail_id"]) in tails,
                f"关系端点类型错误: {rel['relation_id']}")
    snapshot = hashlib.sha256(json_text(hashes).encode("utf-8")).hexdigest()
    return Dataset(data_dir, tables, node_types, snapshot, hashes)


def runtime_dataset(settings: Settings) -> Dataset:
    try:
        dataset = load_dataset(settings.data_dir)
    except (ValueError, OSError) as exc:
        raise DataVersionError("正式数据未通过 manifest 或引用校验，请检查 data 目录。") from exc
    if settings.snapshot and settings.snapshot != dataset.snapshot:
        raise DataVersionError("QA_SNAPSHOT 与正式 JSONL 的内容快照不一致。")
    return dataset
