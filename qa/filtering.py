"""SQL 查询后再次核对硬条件；演示适配器也使用同一业务规则。"""

from datetime import datetime, timedelta, timezone

from .config import Settings
from .schemas import ProductFilters


def eligible_price(row: dict, product: dict, settings: Settings, now: datetime | None = None) -> bool:
    if not settings.price_enabled:
        return False
    if not all(row.get(k) for k in ("current_retail_eligible", "strict_price_filter_eligible")) or row.get("conflict"):
        return False
    if row.get("currency") != "CNY" or row.get("unit") != "bottle" or row.get("pack_quantity") not in (None, 1):
        return False
    if row.get("amount_min") is None or row["amount_min"] <= 0 or row["amount_min"] != row.get("amount_max"):
        return False
    if product.get("record_scope") != "specific_variant" or product.get("vintage_status") not in {"specified", "non_vintage"}:
        return False
    if not product.get("field_filter_eligibility", {}).get("volume_ml") or not product.get("volume_ml"):
        return False
    try:
        verified = datetime.fromisoformat(row.get("verified_at") or "")
    except (TypeError, ValueError):
        return False
    # 无时区的核验时间按中国本地时间解释，与数据采集约定一致。
    if verified.tzinfo is None:
        from zoneinfo import ZoneInfo

        verified = verified.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=settings.price_max_age_days) <= verified <= now


def matches_filters(product: dict, filters: ProductFilters) -> bool:
    if not product.get("strict_product_filter_eligible"):
        return False
    eligibility = product.get("field_filter_eligibility", {})
    for name in ("color", "sweetness", "volume_ml", "vintage", "region_id", "is_sparkling"):
        value = getattr(filters, name)
        if value is not None and (not eligibility.get(name) or product.get(name) is None or product[name] != value):
            return False
    if filters.winery_id is not None and product.get("winery_id") != filters.winery_id:
        return False
    if filters.alcohol_max is not None:
        if not eligibility.get("alcohol_pct_vol") or product.get("alcohol_pct_vol") is None or product["alcohol_pct_vol"] > filters.alcohol_max:
            return False
    if filters.grape_id:
        grapes = product.get("grape_varieties", [])
        if not eligibility.get("grape_varieties") or not any(g["entity_id"] == filters.grape_id for g in grapes):
            return False
        if filters.grape_only and not (product.get("grapes_complete") and len(grapes) == 1 and grapes[0].get("proportion_pct") == 100):
            return False
    if filters.price_min is not None or filters.price_max is not None:
        price = product.get("eligible_price", {}).get("amount_min")
        if price is None or (filters.price_min is not None and price < filters.price_min) or (filters.price_max is not None and price > filters.price_max):
            return False
    return True

