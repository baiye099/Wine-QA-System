"""从正式JSONL离线生成MySQL写入及校验SQL；不连接或执行数据库。"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from dataset import ROOT, Dataset, json_text, load_dataset, normalized_alias


@dataclass
class Table:
    name: str
    columns: list[str]
    primary: set[str]
    json_columns: set[str]
    rows: list[dict]


def build_tables(ds: Dataset) -> list[Table]:
    t = ds.tables
    result = []

    def table(name, columns, primary, json_columns, records):
        rows = [{c: r if c == "raw_record" else r.get(c) for c in columns} for r in records]
        result.append(Table(name, columns, set(primary), set(json_columns), rows))

    table("source_document", ["source_id", "title", "publisher", "url", "source_type", "evidence_form", "published_at", "accessed_date", "raw_file", "raw_record"], ["source_id"], ["raw_record"], t["sources"])
    # catalog中的raw_record已明确指向原始规范对象，不能递归包裹catalog本身。
    registry = [{"entity_id": r["entity_id"], "entity_type": r["entity_type"], "name": r["name"],
                 "source_ids": r["source_ids"], "raw_record": r["raw_record"]} for r in ds.catalog]
    result.append(Table("entity_registry", ["entity_id", "entity_type", "name", "source_ids", "raw_record"], {"entity_id"}, {"source_ids", "raw_record"}, registry))
    table("region", ["region_id", "name", "region_kind"], ["region_id"], [],
          [{**r, "region_id": r["entity_id"], "region_kind": r.get("region_kind") or "unknown"} for r in t["entities"] if r["entity_type"] == "Region"])
    table("winery", ["winery_id", "name", "company_name", "region_id", "location_raw", "address_raw", "address_normalized", "founded_year", "company_founded_year", "introduction", "website", "telephone", "email", "source_ids", "raw_record"], ["winery_id"], ["source_ids", "raw_record"], t["wineries"])
    table("grape_variety", ["grape_id", "name"], ["grape_id"], [],
          [{**r, "grape_id": r["entity_id"]} for r in t["entities"] if r["entity_type"] == "GrapeVariety"])
    table("product", ["product_id", "winery_id", "name", "series", "record_scope", "vintage", "vintage_status", "volume_ml", "color", "sweetness", "sweetness_raw", "is_sparkling", "alcohol_pct_vol", "region_id", "grapes_complete", "description", "price_cny", "strict_product_filter_eligible", "field_filter_eligibility", "field_filter_reasons", "source_ids", "raw_record"],
          ["product_id"], ["field_filter_eligibility", "field_filter_reasons", "source_ids", "raw_record"], t["products"])
    table("product_grape", ["product_id", "grape_id", "proportion_pct", "source_ids", "raw_record"], ["product_id", "grape_id"], ["source_ids", "raw_record"],
          [{**r, "product_id": r["head_id"], "grape_id": r["tail_id"]} for r in t["relations"] if r["relation_type"] == "MADE_FROM"])
    table("price_observation", ["price_id", "product_id", "source_id", "amount_min", "amount_max", "currency", "unit", "pack_quantity", "price_type", "price_type_raw", "quote_date", "quote_date_precision", "observed_date", "verified_at", "minimum_order_raw", "tax_shipping_note", "current_retail_eligible", "strict_price_filter_eligible", "conflict", "display_notice", "raw_record"], ["price_id"], ["raw_record"], t["price_observations"])
    table("entity_alias", ["alias_id", "alias", "alias_key", "entity_id", "entity_type", "alias_kind", "ambiguous", "source_ids", "raw_record"], ["alias_id"], ["source_ids", "raw_record"],
          [{**r, "alias_key": normalized_alias(r["alias"])} for r in t["aliases"]])
    table("professional_review", ["review_id", "product_id", "source_id", "review_type", "summary", "score", "score_scale", "score_raw", "medal", "competition_year", "fact_scope", "raw_record"], ["review_id"], ["raw_record"], t["professional_reviews"])
    return result


def sql_value(value, is_json=False) -> str:
    if value is None:
        return "NULL"
    if is_json:
        payload = json_text(value).encode("utf-8").hex()
        return f"CAST(CONVERT(X'{payload}' USING utf8mb4) AS JSON)"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("不能写入非有限数值")
        return str(value)
    if not isinstance(value, str):
        raise TypeError(f"未声明为JSON的复杂值: {type(value).__name__}")
    return f"CONVERT(X'{value.encode('utf-8').hex()}' USING utf8mb4)"


def render_data(ds: Dataset, tables: list[Table], database: str, batch_size: int) -> str:
    sql = ["-- 自动生成：仅正式accepted数据；由用户执行。", "-- UTF-8十六进制文本不受单引号、反斜杠和客户端SQL模式影响。",
           f"-- 数据快照SHA256: {ds.snapshot}", f"USE `{database}`;", "SET NAMES utf8mb4;", "START TRANSACTION;", ""]
    for table in tables:
        sql.append(f"-- {table.name}: {len(table.rows)} rows")
        for start in range(0, len(table.rows), batch_size):
            chunk = table.rows[start:start + batch_size]
            cols = ", ".join(f"`{c}`" for c in table.columns)
            values = ["(" + ", ".join(sql_value(r[c], c in table.json_columns) for c in table.columns) + ")" for r in chunk]
            updates = ", ".join(f"`{c}` = incoming.`{c}`" for c in table.columns if c not in table.primary)
            sql.extend([f"INSERT INTO `{table.name}` ({cols}) VALUES", ",\n".join(values), "AS incoming", f"ON DUPLICATE KEY UPDATE {updates};", ""])
    sql.extend(["COMMIT;", "-- 完成后执行03_verify_mysql.sql。"])
    return "\n".join(sql) + "\n"


def render_verify(tables: list[Table], database: str) -> str:
    counts = "\nUNION ALL\n".join(f"SELECT '{t.name}' AS table_name, {len(t.rows)} AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `{t.name}`" for t in tables)
    return f"""-- 只读校验与查询示例；首次导入空库时actual_count应等于expected。
USE `{database}`;
{counts};

-- 外键与别名类型检查；均应为0。
SELECT COUNT(*) AS orphan_products FROM product p LEFT JOIN winery w ON p.winery_id=w.winery_id WHERE w.winery_id IS NULL;
SELECT COUNT(*) AS invalid_alias_type FROM entity_alias a JOIN entity_registry e ON a.entity_id=e.entity_id WHERE a.entity_type<>e.entity_type;
SELECT COUNT(*) AS invalid_grape_registry FROM grape_variety g JOIN entity_registry e ON e.entity_id=g.grape_id WHERE e.entity_type<>'GrapeVariety';
SELECT COUNT(*) AS invalid_region_registry FROM region r JOIN entity_registry e ON e.entity_id=r.region_id WHERE e.entity_type<>'Region';

-- 当前批次应为0，参考报价不能被误当成可购买零售价。
SELECT COUNT(*) AS eligible_retail_price_count FROM v_eligible_retail_price;

-- 产品条件筛选：750ml干红；每个条件必须同时通过字段资格。
-- 当前数据快照应得到9条产品记录。
SELECT product_id,name,winery_name,vintage,vintage_status,volume_ml
FROM v_product_basic
WHERE strict_product_filter_eligible=1 AND color='red' AND sweetness='dry' AND volume_ml=750
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.color'))='true'
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.sweetness'))='true'
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.volume_ml'))='true'
ORDER BY product_id;

-- 查询明确披露含赤霞珠的产品，不推断100%配比。
SELECT p.product_id,p.name,p.vintage,p.vintage_status,g.name AS grape_name,pg.proportion_pct
FROM product p JOIN product_grape pg ON pg.product_id=p.product_id
JOIN grape_variety g ON g.grape_id=pg.grape_id
WHERE g.name='赤霞珠' AND p.strict_product_filter_eligible=1
  AND JSON_UNQUOTE(JSON_EXTRACT(p.field_filter_eligibility,'$.grape_varieties'))='true'
ORDER BY p.product_id;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--database", default="Wine_QA_System")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if not args.database.isascii() or not args.database.replace("_", "a").isalnum() or len(args.database) > 64:
        parser.error("数据库名仅允许ASCII字母、数字、下划线，长度1-64")
    if args.batch_size < 1:
        parser.error("batch-size必须大于0")
    ds = load_dataset(args.data_dir)
    tables = build_tables(ds)
    output = args.output_dir.resolve()
    for protected in [ds.data_dir, ROOT / "data_raw"]:
        if output == protected.resolve() or protected.resolve() in output.parents:
            parser.error("SQL输出目录不能位于data或data_raw内，请使用database目录")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).resolve().parent / "01_schema_mysql.sql"
    schema = schema_path.read_text(encoding="utf-8")
    schema = re.sub(r"(CREATE DATABASE IF NOT EXISTS|USE) `[^`]+`", lambda m: m[1] + f" `{args.database}`", schema)
    (args.output_dir / "01_schema_mysql.sql").write_text(schema, encoding="utf-8", newline="\n")
    (args.output_dir / "02_data_mysql.sql").write_text(render_data(ds, tables, args.database, args.batch_size), encoding="utf-8", newline="\n")
    (args.output_dir / "03_verify_mysql.sql").write_text(render_verify(tables, args.database), encoding="utf-8", newline="\n")
    print(json.dumps({"database": args.database, "snapshot": ds.snapshot, "table_rows": {t.name: len(t.rows) for t in tables},
                      "executed_database": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
