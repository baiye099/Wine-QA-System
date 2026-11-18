"""正式数据到 MySQL 列的映射，供只读校验、SQL 生成和入库共用。"""

from dataclasses import dataclass

from ..core.text import normalized_alias
from ..models.dataset import Dataset


@dataclass
class Table:
    name: str
    columns: list[str]
    primary: set[str]
    json_columns: set[str]
    rows: list[dict]


def build_tables(ds: Dataset) -> list[Table]:
    data = ds.tables
    result = []

    def table(name, columns, primary, json_columns, records):
        rows = [{c: r if c == "raw_record" else r.get(c) for c in columns} for r in records]
        result.append(Table(name, columns, set(primary), set(json_columns), rows))

    table("source_document", ["source_id", "title", "publisher", "url", "source_type", "evidence_form",
                              "published_at", "accessed_date", "raw_file", "raw_record"],
          ["source_id"], ["raw_record"], data["sources"])
    # 实体目录已携带原始规范记录，直接使用其 raw_record。
    result.append(Table("entity_registry", ["entity_id", "entity_type", "name", "source_ids", "raw_record"],
                        {"entity_id"}, {"source_ids", "raw_record"}, ds.catalog))
    table("region", ["region_id", "name", "region_kind"], ["region_id"], [],
          [{**r, "region_id": r["entity_id"], "region_kind": r.get("region_kind") or "unknown"}
           for r in data["entities"] if r["entity_type"] == "Region"])
    table("winery", ["winery_id", "name", "company_name", "region_id", "location_raw", "address_raw",
                     "address_normalized", "founded_year", "company_founded_year", "introduction", "website",
                     "telephone", "email", "source_ids", "raw_record"],
          ["winery_id"], ["source_ids", "raw_record"], data["wineries"])
    table("grape_variety", ["grape_id", "name"], ["grape_id"], [],
          [{**r, "grape_id": r["entity_id"]} for r in data["entities"] if r["entity_type"] == "GrapeVariety"])
    table("product", ["product_id", "winery_id", "name", "series", "record_scope", "vintage", "vintage_status",
                      "volume_ml", "color", "sweetness", "sweetness_raw", "is_sparkling", "alcohol_pct_vol", "region_id",
                      "grapes_complete", "description", "price_cny", "strict_product_filter_eligible",
                      "field_filter_eligibility", "field_filter_reasons", "source_ids", "raw_record"],
          ["product_id"], ["field_filter_eligibility", "field_filter_reasons", "source_ids", "raw_record"], data["products"])
    table("product_grape", ["product_id", "grape_id", "proportion_pct", "source_ids", "raw_record"],
          ["product_id", "grape_id"], ["source_ids", "raw_record"],
          [{**r, "product_id": r["head_id"], "grape_id": r["tail_id"]}
           for r in data["relations"] if r["relation_type"] == "MADE_FROM"])
    table("price_observation", ["price_id", "product_id", "source_id", "amount_min", "amount_max", "currency", "unit",
                                "pack_quantity", "price_type", "price_type_raw", "quote_date", "quote_date_precision",
                                "observed_date", "verified_at", "minimum_order_raw", "tax_shipping_note",
                                "current_retail_eligible", "strict_price_filter_eligible", "conflict", "display_notice", "raw_record"],
          ["price_id"], ["raw_record"], data["price_observations"])
    table("entity_alias", ["alias_id", "alias", "alias_key", "entity_id", "entity_type", "alias_kind", "ambiguous",
                           "source_ids", "raw_record"], ["alias_id"], ["source_ids", "raw_record"],
          [{**r, "alias_key": normalized_alias(r["alias"])} for r in data["aliases"]])
    table("professional_review", ["review_id", "product_id", "source_id", "review_type", "summary", "score",
                                   "score_scale", "score_raw", "medal", "competition_year", "fact_scope", "raw_record"],
          ["review_id"], ["raw_record"], data["professional_reviews"])
    return result
