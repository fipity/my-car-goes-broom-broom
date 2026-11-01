"""Valuation settings from config/valuation.yaml (re-read on every recompute)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class LabelRule(BaseModel):
    label: str
    below: float | None  # upper bound on diff %, None = everything else


class Tier(BaseModel):
    name: str
    same_variant: bool
    same_specs: bool  # transmission + body must match where both are known
    years: int
    km_band: float | None  # +/- fraction of the target's km; None = any


class RegressionConfig(BaseModel):
    min_listings: int = 30
    min_r2: float = 0.5
    min_category_count: int = 5
    ridge: float = 1.0
    max_disagreement: float = 0.25


class ConfidenceLevel(BaseModel):
    min_comps: int
    max_spread: float


class JunkConfig(BaseModel):
    min_price: int = 500
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    negations: list[str] = Field(default_factory=list)


class ValuationConfig(BaseModel):
    labels: list[LabelRule] = Field(default_factory=lambda: [
        LabelRule(label="Great deal", below=-15), LabelRule(label="Good deal", below=-5),
        LabelRule(label="Fair", below=5), LabelRule(label="Above market", below=15),
        LabelRule(label="Overpriced", below=None)])
    drive_away_to_egc: dict[str, float] = Field(default_factory=lambda: {"default": 0.96})
    min_comps: int = 6
    sold_max_age_days: int = 180
    max_comps_shown: int = 20
    tiers: list[Tier] = Field(default_factory=lambda: [
        Tier(name="close", same_variant=True, same_specs=True, years=2, km_band=0.4),
        Tier(name="specs", same_variant=False, same_specs=True, years=2, km_band=0.4),
        Tier(name="model", same_variant=False, same_specs=False, years=2, km_band=0.6),
        Tier(name="wide", same_variant=False, same_specs=False, years=4, km_band=None)])
    km_band_min: int = 30000
    iqr_k: float = 1.5
    regression: RegressionConfig = Field(default_factory=RegressionConfig)
    confidence: dict[str, ConfidenceLevel] = Field(default_factory=lambda: {
        "high": ConfidenceLevel(min_comps=15, max_spread=0.20),
        "medium": ConfidenceLevel(min_comps=6, max_spread=0.35)})
    junk: JunkConfig = Field(default_factory=JunkConfig)

    def egc_factor(self, state: str | None) -> float:
        return self.drive_away_to_egc.get(state or "", self.drive_away_to_egc.get("default", 1.0))


def load_valuation_config(path: Path | None = None) -> ValuationConfig:
    if path is None:
        from carscout.core.config import get_settings

        path = get_settings().valuation_file
    if not path.exists():
        return ValuationConfig()
    return ValuationConfig.model_validate(yaml.safe_load(path.read_text()) or {})
