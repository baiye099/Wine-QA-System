"""与离线发布脚本共用数据指纹，不读取 review/excluded/auxiliary。"""

from database.dataset import Dataset, load_dataset, normalized_alias

from .config import Settings
from .errors import DataVersionError


def runtime_dataset(settings: Settings) -> Dataset:
    try:
        dataset = load_dataset(settings.data_dir)
    except (ValueError, OSError) as exc:
        raise DataVersionError("正式数据未通过 manifest 或引用校验，请检查 data 目录。") from exc
    if settings.snapshot and settings.snapshot != dataset.snapshot:
        raise DataVersionError("QA_SNAPSHOT 与正式 JSONL 的内容快照不一致。")
    return dataset


def mysql_expected_tables(ds: Dataset) -> dict[str, list[dict]]:
    """对应现有 SQL 生成器的行；验证真实列与 raw_record，拒绝遗留多余行。"""
    tables = {}
    for table, source in {"source_document": "sources", "winery": "wineries", "product": "products",
                          "price_observation": "price_observations", "professional_review": "professional_reviews"}.items():
        tables[table] = [{**r, "raw_record": r} for r in ds.tables[source]]
    tables["entity_registry"] = ds.catalog
    tables["entity_alias"] = []
    for r in ds.tables["aliases"]:
        # SQL 生成器先补 alias_key，再将该规范化记录整体保存为 raw_record。
        row = {**r, "alias_key": normalized_alias(r["alias"])}
        tables["entity_alias"].append({**row, "raw_record": row})
    tables["product_grape"] = []
    for r in ds.tables["relations"]:
        if r["relation_type"] == "MADE_FROM":
            row = {**r, "product_id": r["head_id"], "grape_id": r["tail_id"]}
            tables["product_grape"].append({**row, "raw_record": row})
    tables["region"] = [{"region_id": r["entity_id"], "name": r["name"], "region_kind": r.get("region_kind") or "unknown"}
                        for r in ds.tables["entities"] if r["entity_type"] == "Region"]
    tables["grape_variety"] = [{"grape_id": r["entity_id"], "name": r["name"]}
                               for r in ds.tables["entities"] if r["entity_type"] == "GrapeVariety"]
    return tables

