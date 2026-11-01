"""Market value of a listing from comparable listings. Pure functions: no database, no network.

For one target listing:
1. Comparables: same make + model, one listing per physical car, junk already removed. Criteria
   widen tier by tier (config `tiers`) until enough comps survive an IQR outlier filter.
2. Two estimates, both on an "excluding government charges" price basis (drive-away prices are
   scaled by a per-state factor):
   - comps: weighted median of the comparables (closer year/km/variant = more weight), with the
     weighted 25th-75th percentiles as the range;
   - regression: log(price) ~ age + age^2 + km + km^2 + manual + private seller + variant + body,
     ridge-regularised, fitted per make/model on all its cars. Used when the group is big enough,
     fits well (R^2) and the target is inside the data it was fitted on.
3. Regression wins when usable, else the comps median. The price difference gives the deal label.
   Confidence comes from how much data is behind the estimate (the fitted group for a regression,
   the comps otherwise) and its spread (25th-75th percentile range / estimate). It's lowered a
   level when the comp criteria had to be widened, and again when the two estimates disagree.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from carscout.dedupe.match import variant_similarity
from carscout.valuation.config import Tier, ValuationConfig


@dataclass(frozen=True)
class Item:
    id: int
    vehicle_id: int | None = None
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    year: int | None = None
    odometer_km: int | None = None
    price: int | None = None
    price_type: str = "unknown"
    state: str | None = None
    transmission: str | None = None
    body_type: str | None = None
    seller_type: str = "unknown"
    is_active: bool = True
    last_seen: datetime | None = None

    @property
    def car_key(self) -> int:
        return self.vehicle_id if self.vehicle_id is not None else -self.id


@dataclass
class Result:
    estimate: int | None            # on the target's own price basis (drive-away or EGC)
    p25: int | None
    p75: int | None
    diff_abs: int | None            # price - estimate (negative = cheaper than market)
    diff_pct: float | None
    label: str | None
    confidence: str | None          # High | Medium | Low
    method: str                     # regression | comps | insufficient
    n_comps: int
    comp_ids: list[int]
    details: dict[str, Any] = field(default_factory=dict)


def egc_price(item: Item, cfg: ValuationConfig) -> float | None:
    if item.price is None:
        return None
    if item.price_type == "drive_away":
        return item.price * cfg.egc_factor(item.state)
    return float(item.price)


def _norm(s: str | None) -> str | None:
    return "".join(ch for ch in s.lower() if ch.isalnum()) if s else None


def _clash(a: str | None, b: str | None) -> bool:
    return bool(a and b and _norm(a) != _norm(b))


# ---- statistics helpers ------------------------------------------------------------------
def weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(np.quantile([v for v, _ in pairs], q))
    # midpoint convention: each value owns the middle of its weight slice
    acc = 0.0
    points = []
    for v, w in pairs:
        points.append(((acc + w / 2) / total, v))
        acc += w
    if q <= points[0][0]:
        return points[0][1]
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if q <= q1:
            return v0 + (v1 - v0) * (q - q0) / (q1 - q0) if q1 > q0 else v1
    return points[-1][1]


def iqr_keep(values: list[float], k: float) -> list[bool]:
    if len(values) < 4:
        return [True] * len(values)
    q1, q3 = np.quantile(values, [0.25, 0.75])
    lo, hi = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
    return [lo <= v <= hi for v in values]


# ---- comparables ---------------------------------------------------------------------------
def _comp_weight(t: Item, c: Item) -> float:
    w = math.exp(-abs((t.year or 0) - (c.year or 0)) / 2.0)
    if t.odometer_km is not None and c.odometer_km is not None:
        w *= math.exp(-abs(t.odometer_km - c.odometer_km) / max(30000.0, 0.5 * t.odometer_km))
    sim = variant_similarity(t.variant, c.variant)
    if sim is not None and sim >= 0.5:
        w *= 1.3
    if t.transmission and _norm(t.transmission) == _norm(c.transmission):
        w *= 1.1
    if t.body_type and _norm(t.body_type) == _norm(c.body_type):
        w *= 1.1
    if not c.is_active:
        w *= 0.8  # sold listings: last asking price, a little older
    return w


def _in_tier(t: Item, c: Item, tier: Tier, cfg: ValuationConfig) -> bool:
    if t.year is None or c.year is None or abs(t.year - c.year) > tier.years:
        return False
    if tier.km_band is not None and t.odometer_km is not None:
        if c.odometer_km is None:
            return False
        band = max(tier.km_band * t.odometer_km, cfg.km_band_min)
        if abs(c.odometer_km - t.odometer_km) > band:
            return False
    if tier.same_specs and (_clash(t.transmission, c.transmission) or _clash(t.body_type, c.body_type)):
        return False
    if tier.same_variant:
        sim = variant_similarity(t.variant, c.variant)
        if sim is None or sim < 0.5:
            return False
    return True


def select_comps(target: Item, pool: list[Item], cfg: ValuationConfig
                 ) -> tuple[str | None, list[tuple[Item, float]], int]:
    """(tier name, [(comp, weight)] sorted by weight, outliers removed). Pool must already be
    one listing per car, without junk; the target's own car is skipped here."""
    candidates = [c for c in pool if c.car_key != target.car_key and egc_price(c, cfg)]
    last: tuple[str | None, list[tuple[Item, float]], int] = (None, [], 0)
    for tier in cfg.tiers:
        comps = [c for c in candidates if _in_tier(target, c, tier, cfg)]
        logs = [math.log(egc_price(c, cfg)) for c in comps]
        keep = iqr_keep(logs, cfg.iqr_k)
        kept = [c for c, k in zip(comps, keep) if k]
        weighted = sorted(((c, _comp_weight(target, c)) for c in kept), key=lambda p: -p[1])
        last = (tier.name, weighted, len(comps) - len(kept))
        if len(kept) >= cfg.min_comps:
            return last
    return last


# ---- regression ------------------------------------------------------------------------------
@dataclass
class GroupModel:
    beta: np.ndarray
    mu: np.ndarray
    sd: np.ndarray
    variants: list[str]
    bodies: list[str]
    ref_year: int
    r2: float
    n: int
    resid_q25: float
    resid_q75: float
    year_range: tuple[int, int]
    km_max: int

    def features(self, it: Item) -> np.ndarray | None:
        if it.year is None or it.odometer_km is None:
            return None
        age = max(0.0, self.ref_year - it.year + 0.5)
        km = it.odometer_km / 10000.0
        row = [age, age * age, km, km * km,
               1.0 if _norm(it.transmission) == "manual" else 0.0,
               1.0 if it.seller_type == "private" else 0.0]
        v, b = _norm(it.variant), _norm(it.body_type)
        row += [1.0 if v == x else 0.0 for x in self.variants]
        row += [1.0 if b == x else 0.0 for x in self.bodies]
        return np.array(row)

    def eligible(self, it: Item) -> bool:
        return (it.year is not None and it.odometer_km is not None
                and self.year_range[0] - 1 <= it.year <= self.year_range[1] + 1
                and it.odometer_km <= self.km_max * 1.2)

    def predict_log(self, it: Item) -> float | None:
        x = self.features(it)
        if x is None:
            return None
        return float(self.beta[0] + ((x - self.mu) / self.sd) @ self.beta[1:])

    def effects(self, it: Item) -> dict[str, float]:
        """% price effect of +1 year of age and +10,000 km at the target's point, for display."""
        base = self.predict_log(it)
        if base is None:
            return {}
        older = Item(**{**it.__dict__, "year": it.year - 1})
        more = Item(**{**it.__dict__, "odometer_km": it.odometer_km + 10000})
        return {"per_year_pct": round((math.exp(self.predict_log(older) - base) - 1) * 100, 1),
                "per_10k_km_pct": round((math.exp(self.predict_log(more) - base) - 1) * 100, 1)}


def _solve(X: np.ndarray, y: np.ndarray, ridge: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    A = np.hstack([np.ones((len(Z), 1)), Z])
    penalty = ridge * np.eye(A.shape[1])
    penalty[0, 0] = 0.0  # don't shrink the intercept
    beta = np.linalg.solve(A.T @ A + penalty, A.T @ y)
    return beta, mu, sd


def fit_group(pool: list[Item], cfg: ValuationConfig, ref_year: int) -> GroupModel | None:
    rc = cfg.regression
    data = [(it, egc_price(it, cfg)) for it in pool
            if it.year is not None and it.odometer_km is not None and egc_price(it, cfg)]
    if len(data) < rc.min_listings:
        return None

    def common(values: list[str | None]) -> list[str]:
        counts: dict[str, int] = {}
        for v in values:
            if v:
                counts[v] = counts.get(v, 0) + 1
        ranked = sorted((k for k, n in counts.items() if n >= rc.min_category_count), key=lambda k: -counts[k])
        return ranked[1:]  # the most common one is the baseline

    shell = GroupModel(beta=np.zeros(1), mu=np.zeros(1), sd=np.ones(1),
                       variants=common([_norm(it.variant) for it, _ in data]),
                       bodies=common([_norm(it.body_type) for it, _ in data]),
                       ref_year=ref_year, r2=0.0, n=0, resid_q25=0.0, resid_q75=0.0,
                       year_range=(min(it.year for it, _ in data), max(it.year for it, _ in data)),
                       km_max=max(it.odometer_km for it, _ in data))
    X = np.array([shell.features(it) for it, _ in data])
    y = np.log([p for _, p in data])

    beta, mu, sd = _solve(X, y, rc.ridge)
    resid = y - (beta[0] + ((X - mu) / sd) @ beta[1:])
    keep = np.array(iqr_keep(list(resid), cfg.iqr_k))  # refit without gross outliers
    if keep.sum() >= rc.min_listings and not keep.all():
        X, y = X[keep], y[keep]
        beta, mu, sd = _solve(X, y, rc.ridge)
        resid = y - (beta[0] + ((X - mu) / sd) @ beta[1:])
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    q25, q75 = np.quantile(resid, [0.25, 0.75])
    shell.beta, shell.mu, shell.sd = beta, mu, sd
    shell.r2, shell.n, shell.resid_q25, shell.resid_q75 = r2, len(y), float(q25), float(q75)
    return shell


# ---- putting it together -----------------------------------------------------------------------
def deal_label(diff_pct: float, cfg: ValuationConfig) -> str:
    for rule in cfg.labels:
        if rule.below is None or diff_pct < rule.below:
            return rule.label
    return cfg.labels[-1].label


def _confidence(n: int, spread: float, cfg: ValuationConfig) -> str:
    hi, med = cfg.confidence.get("high"), cfg.confidence.get("medium")
    if hi and n >= hi.min_comps and spread <= hi.max_spread:
        return "High"
    if med and n >= med.min_comps and spread <= med.max_spread:
        return "Medium"
    return "Low"


_LOWER = {"High": "Medium", "Medium": "Low", "Low": "Low"}


def value(target: Item, pool: list[Item], cfg: ValuationConfig, group: GroupModel | None) -> Result:
    tier, comps, outliers = select_comps(target, pool, cfg)
    details: dict[str, Any] = {"tier": tier, "outliers_removed": outliers,
                               "price_basis": "drive-away" if target.price_type == "drive_away" else "excl. govt charges"}
    factor = cfg.egc_factor(target.state) if target.price_type == "drive_away" else 1.0
    if target.price_type == "drive_away":
        details["egc_factor"] = factor

    comp_est = comp_p25 = comp_p75 = None
    if len(comps) >= 3:
        vals = [egc_price(c, cfg) for c, _ in comps]
        ws = [w for _, w in comps]
        comp_est, comp_p25, comp_p75 = (weighted_quantile(vals, ws, q) for q in (0.5, 0.25, 0.75))
        details["comps_estimate"] = round(comp_est / factor)

    reg_est = reg_p25 = reg_p75 = None
    if group is not None and group.r2 >= cfg.regression.min_r2 and group.eligible(target):
        lp = group.predict_log(target)
        q25, q75 = group.resid_q25, group.resid_q75
        # Spread varies across the market (old cars' prices scatter far more than new ones'), so
        # take the range from how the nearby comparables sit around the model, when there are enough.
        local = [(math.log(egc_price(c, cfg)) - group.predict_log(c), w) for c, w in comps
                 if group.predict_log(c) is not None]
        if len(local) >= 5:
            q25 = min(q25, weighted_quantile([r for r, _ in local], [w for _, w in local], 0.25))
            q75 = max(q75, weighted_quantile([r for r, _ in local], [w for _, w in local], 0.75))
            details["local_spread"] = True
        reg_est, reg_p25, reg_p75 = (math.exp(lp + d) for d in (0.0, q25, q75))
        details.update(regression_estimate=round(reg_est / factor), r2=round(group.r2, 3),
                       group_size=group.n, **group.effects(target))

    if reg_est is not None:
        method, est, p25, p75 = "regression", reg_est, reg_p25, reg_p75
    elif comp_est is not None:
        method, est, p25, p75 = "comps", comp_est, comp_p25, comp_p75
    else:
        details["reason"] = f"only {len(comps)} comparable car(s) found"
        return Result(None, None, None, None, None, None, None, "insufficient", len(comps),
                      [c.id for c, _ in comps], details)

    spread = (p75 - p25) / est if est else 1.0
    # Evidence behind the number: the whole fitted group for a regression, the comps otherwise.
    evidence = group.n if method == "regression" else len(comps)
    confidence = _confidence(evidence, spread, cfg)
    tier_names = [t.name for t in cfg.tiers]
    if tier in tier_names[2:]:
        confidence = _LOWER[confidence]
        details["widened"] = True
    if reg_est is not None and comp_est is not None:
        gap = abs(reg_est - comp_est) / comp_est
        details["estimates_gap_pct"] = round(gap * 100, 1)
        if gap > cfg.regression.max_disagreement:
            confidence = _LOWER[confidence]

    estimate = round(est / factor)
    result = Result(estimate=estimate, p25=round(p25 / factor), p75=round(p75 / factor),
                    diff_abs=None, diff_pct=None, label=None, confidence=confidence, method=method,
                    n_comps=len(comps), comp_ids=[c.id for c, _ in comps[: cfg.max_comps_shown]],
                    details=details)
    if target.price:
        result.diff_abs = target.price - estimate
        result.diff_pct = round(result.diff_abs / estimate * 100, 1)
        result.label = deal_label(result.diff_pct, cfg)
    return result
