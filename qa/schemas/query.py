"""问题理解、实体定位与产品筛选的结构化契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .base import Contract

EntityType = Literal["Winery", "Product", "Region", "GrapeVariety", "Flavor", "Concept"]
Intent = Literal["winery", "product", "filter", "knowledge", "clarify"]
BusinessID = str
FilterName = Literal["color", "sweetness", "volume_ml", "vintage", "winery_id", "region_id", "grape_id",
                     "grape_only", "is_sparkling", "alcohol_max", "price_min", "price_max"]


class EntityRef(Contract):
    entity_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    entity_type: EntityType
    name: str


class ProductFilters(Contract):
    color: Literal["red", "white", "rose", "other"] | None = None
    sweetness: Literal["dry", "semi_dry", "semi_sweet", "sweet", "other"] | None = None
    volume_ml: float | None = Field(default=None, gt=0, le=100000)
    vintage: int | None = Field(default=None, ge=1000, le=2200)
    winery_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    region_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    grape_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    grape_only: StrictBool | None = None
    is_sparkling: StrictBool | None = None
    alcohol_max: float | None = Field(default=None, ge=0, le=100)
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_ranges(self):
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("价格下限大于上限")
        if self.grape_only and not self.grape_id:
            raise ValueError("100% 品种筛选需要指定品种")
        return self

    def active(self) -> dict:
        return self.model_dump(exclude_none=True)


class ParsedQuery(Contract):
    intent: Intent
    filters: ProductFilters = Field(default_factory=ProductFilters)
    remove_filters: list[FilterName] = Field(default_factory=list)
    inherit_filters: bool = False
    entity_ids: list[str] = Field(default_factory=list, max_length=10)
    result_position: int | None = Field(default=None, ge=1, le=50)
    references_current: bool = False
    scope: Literal["all", "last_results"] = "all"
    preferences: list[str] = Field(default_factory=list, max_length=10)
    category: Literal["wine_basics", "tasting", "storage", "serving", "pairing", "winery_intro", "product_intro"] | None = None
    clarification: str | None = None
    notices: list[str] = Field(default_factory=list)

