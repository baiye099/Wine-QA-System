"""现有 MySQL 表的只读适配器；参数值与 SQL 模板分离。"""

from __future__ import annotations

import time

from ..core.config import Settings
from ..core.errors import DataVersionError
from ..db.mysql import decode_row, mysql_read_connection
from ..db.mysql_tables import build_tables
from ..models.dataset import Dataset
from ..models.product_rules import attach_prices, matches_filters
from ..schemas.query import EntityRef, ProductFilters
from ..schemas.response import Source


def compile_product_query(filters: ProductFilters, product_ids: list[str] | None, limit: int) -> tuple[str, list]:
    """只有常量白名单中的列/JSON路径可拼入语句；用户值一律绑定。"""
    clauses, params = ["p.strict_product_filter_eligible = 1"], []
    for field in ("color", "sweetness", "volume_ml", "vintage", "region_id", "is_sparkling"):
        value = getattr(filters, field)
        if value is not None:
            clauses.extend([f"JSON_UNQUOTE(JSON_EXTRACT(p.field_filter_eligibility, '$.{field}')) = 'true'", f"p.{field} = %s"])
            params.append(value)
    if filters.winery_id:
        clauses.append("p.winery_id = %s")
        params.append(filters.winery_id)
    if filters.alcohol_max is not None:
        clauses.extend(["JSON_UNQUOTE(JSON_EXTRACT(p.field_filter_eligibility, '$.alcohol_pct_vol')) = 'true'", "p.alcohol_pct_vol <= %s"])
        params.append(filters.alcohol_max)
    if filters.grape_id:
        clauses.append("JSON_UNQUOTE(JSON_EXTRACT(p.field_filter_eligibility, '$.grape_varieties')) = 'true'")
        pure = " AND pg.proportion_pct = 100" if filters.grape_only else ""
        clauses.append("EXISTS (SELECT 1 FROM product_grape pg WHERE pg.product_id = p.product_id AND pg.grape_id = %s" + pure + ")")
        params.append(filters.grape_id)
        if filters.grape_only:
            clauses.extend(["p.grapes_complete = 1", "(SELECT COUNT(*) FROM product_grape pg2 WHERE pg2.product_id = p.product_id) = 1"])
    if product_ids is not None:
        if not product_ids:
            clauses.append("1 = 0")
        else:
            clauses.append("p.product_id IN (" + ",".join(["%s"] * len(product_ids)) + ")")
            params.extend(product_ids)
    params.append(limit)
    return "SELECT p.* FROM v_product_basic p WHERE " + " AND ".join(clauses) + " ORDER BY p.product_id LIMIT %s", params


class MySQLRepository:
    def __init__(self, settings: Settings, dataset: Dataset):
        self.settings, self.dataset = settings, dataset
        self._checked_at: float | None = None

    def _read(self, query: str, params=()) -> list[dict]:
        with mysql_read_connection(self.settings) as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            return [decode_row(r) for r in cursor.fetchall()]

    def ensure_ready(self) -> None:
        if self._checked_at is not None and time.monotonic() - self._checked_at < 60:
            return
        # MySQL 现有表无快照列：比对全部正式行和真实查询列，不能凭旧报告宣称版本一致。
        with mysql_read_connection(self.settings) as connection, connection.cursor() as cursor:
            for table in build_tables(self.dataset):
                cursor.execute(f"SELECT * FROM `{table.name}`")
                actual = [decode_row(r) for r in cursor.fetchall()]
                key_columns = sorted(table.primary)
                expected_map = {tuple(row[c] for c in key_columns): row for row in table.rows}
                actual_map = {tuple(row.get(c) for c in key_columns): row for row in actual}
                if len(actual) != len(table.rows) or actual_map.keys() != expected_map.keys():
                    raise DataVersionError(f"MySQL {table.name} 的实体集合与启用的数据快照不一致。")
                for key, row in actual_map.items():
                    want = expected_map[key]
                    changed = [c for c in table.columns if c not in row or row[c] != want[c]]
                    changed.extend(c for c in row if c not in want and row[c] is not None)
                    if changed:
                        record_id = ", ".join(str(v) for v in key)
                        raise DataVersionError(f"MySQL {table.name}（{record_id}）的字段 {', '.join(changed)} 与正式 JSONL 不一致，请核对离线发布。")
        self._checked_at = time.monotonic()

    def aliases(self) -> list[dict]:
        return self._read("SELECT a.alias, e.entity_id, e.entity_type, e.name FROM entity_alias a JOIN entity_registry e ON e.entity_id = a.entity_id")

    def _by_ids(self, template: str, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        return self._read(template.replace("{ids}", ",".join(["%s"] * len(ids))), ids)

    def entities(self, entity_ids: list[str]) -> list[EntityRef]:
        return [EntityRef(**r) for r in self._by_ids("SELECT entity_id, entity_type, name FROM entity_registry WHERE entity_id IN ({ids})", entity_ids)]

    def _attach_grapes(self, rows: list[dict]) -> list[dict]:
        ids = [r["product_id"] for r in rows]
        grapes = self._by_ids("SELECT pg.product_id, pg.grape_id AS entity_id, g.name, pg.proportion_pct, pg.source_ids FROM product_grape pg JOIN grape_variety g ON g.grape_id = pg.grape_id WHERE pg.product_id IN ({ids}) ORDER BY pg.grape_id", ids)
        for row in rows:
            row["grape_varieties"] = [{k: v for k, v in g.items() if k != "product_id"} for g in grapes if g["product_id"] == row["product_id"]]
        return rows

    def _attach_prices(self, rows: list[dict]) -> list[dict]:
        quotes = self._by_ids("SELECT * FROM price_observation WHERE product_id IN ({ids})",
                              [r["product_id"] for r in rows]) if self.settings.price_enabled else []
        attach_prices(rows, quotes, self.settings)
        for row in rows:
            row.pop("raw_record", None)
        return rows

    def products(self, product_ids: list[str]) -> list[dict]:
        rows = self._attach_prices(self._attach_grapes(self._by_ids("SELECT * FROM v_product_basic WHERE product_id IN ({ids})", product_ids)))
        mapped = {r["product_id"]: r for r in rows}
        return [mapped[pid] for pid in dict.fromkeys(product_ids) if pid in mapped]

    def wineries(self, winery_ids: list[str]) -> list[dict]:
        rows = self._by_ids("SELECT * FROM winery WHERE winery_id IN ({ids})", winery_ids)
        for row in rows:
            row.pop("raw_record", None)
        return rows

    def filter_products(self, filters: ProductFilters, product_ids: list[str] | None, limit: int) -> list[dict]:
        has_budget = filters.price_min is not None or filters.price_max is not None
        # 价格资格在Python中检查；必须先筛完预算，再截取展示数量。
        candidate_limit = len(self.dataset.tables["products"]) if has_budget else limit
        query, params = compile_product_query(filters, product_ids, candidate_limit)
        rows = self._attach_prices(self._attach_grapes(self._read(query, params)))
        return [r for r in rows if matches_filters(r, filters)][:limit]

    def sources(self, source_ids: list[str]) -> list[Source]:
        return [Source(**r) for r in self._by_ids("SELECT source_id, title, url, publisher, evidence_form FROM source_document WHERE source_id IN ({ids})", source_ids)]

    def close(self) -> None:
        pass  # 连接按只读事务创建并关闭，不保留用户会话连接。
