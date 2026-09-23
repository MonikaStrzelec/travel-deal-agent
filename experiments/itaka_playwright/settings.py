"""POC-only limits; business thresholds come from the application's config."""

import json
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from travel_deal_agent.config import validate_filters
from travel_deal_agent.config_types import FilterConfig

ROOT = Path(__file__).resolve().parents[2]
POC_CONFIG = Path(__file__).with_name("settings.json")


def load_business_filters(path: Path = ROOT / "config.json") -> FilterConfig:
    filters = TypeAdapter(FilterConfig).validate_python(
        json.loads(path.read_text(encoding="utf-8"))["filters"], strict=True
    )
    validate_filters(filters)
    return filters


class SearchFilters(BaseModel):
    max_price: Decimal = Field(gt=0, allow_inf_nan=False)
    min_stars: int = Field(ge=1, le=5, strict=True)
    currency: str


class BusinessConfig(BaseModel):
    filters: SearchFilters


class PocLimits(BaseModel):
    max_offers: int = Field(gt=0, strict=True)
    max_analyzed_cards: int = Field(default=15, gt=0, strict=True)
    max_scrolls: int = Field(default=20, gt=0, strict=True)
    max_idle_scrolls: int = Field(default=3, gt=0, strict=True)
    scroll_wait_ms: int = Field(default=1500, gt=0, strict=True)


def load_search_settings(
    business_path: Path = ROOT / "config.json",
    poc_path: Path = POC_CONFIG,
) -> tuple[SearchFilters, PocLimits]:
    filters = BusinessConfig.model_validate_json(business_path.read_text(encoding="utf-8")).filters
    limits = PocLimits.model_validate_json(poc_path.read_text(encoding="utf-8"))
    if filters.currency != "PLN":
        raise ValueError("The observed ITAKA UI uses PLN")
    return filters, limits
