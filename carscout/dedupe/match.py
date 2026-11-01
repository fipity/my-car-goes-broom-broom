"""Deciding whether two listings are the same physical car. Pure functions, no database.

Order of evidence:
1. VIN: equal VINs are the same car; two *different* VINs never are, whatever else matches.
2. Rego + state: the same.
3. Fuzzy, only across different sources (a source's own ids are already unique), and only when
   both have an odometer reading: everything that's known on both sides must agree (make, model,
   state, body, transmission, fuel, drivetrain, colour), then year, odometer, price and variant
   are scored. Odometer carries the most weight: two different cars of the same model rarely sit
   within a couple of hundred km of each other.

Images aren't compared: that would mean downloading every photo from the sites' CDNs.

Tune with MatchConfig (see README "Tuning dedupe").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from carscout.normalise.mapping import drivetrain_family, variant_core


@dataclass(frozen=True)
class Car:
    id: int
    source: str
    make: str | None = None
    model: str | None = None
    variant: str | None = None
    year: int | None = None
    odometer_km: int | None = None
    price: int | None = None
    state: str | None = None
    body_type: str | None = None
    transmission: str | None = None
    fuel_type: str | None = None
    drivetrain: str | None = None
    colour: str | None = None
    vin: str | None = None
    rego: str | None = None
    is_active: bool = True
    last_seen: datetime | None = None


@dataclass(frozen=True)
class MatchConfig:
    km_abs: int = 2_000          # odometer tolerance: the larger of this and km_pct of the reading
    km_pct: float = 0.02
    price_pct: float = 0.15      # price tolerance (sites may show drive-away vs EGC, or lag a change)
    year_tolerance: int = 1      # build year vs model year differ by one now and then
    threshold: float = 0.75      # fuzzy score needed to call it the same car
    weights: dict[str, float] = field(default_factory=lambda: {
        "km": 0.45, "year": 0.25, "price": 0.15, "variant": 0.15})


@dataclass(frozen=True)
class Match:
    method: str   # vin | rego | fuzzy
    score: float  # 1.0 for vin/rego


METHOD_RANK = {"vin": 3, "rego": 2, "fuzzy": 1, "single": 0}
_VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_TOKEN = re.compile(r"[a-z0-9]+")


def clean_vin(v: str | None) -> str | None:
    v = (v or "").strip().upper()
    return v if _VIN.match(v) else None


def clean_rego(v: str | None) -> str | None:
    v = re.sub(r"[^A-Z0-9]", "", (v or "").upper())
    return v if 2 <= len(v) <= 9 else None


def _norm(s: str | None) -> str | None:
    return re.sub(r"[^a-z0-9]", "", s.lower()) if s else None


def _conflict(a: str | None, b: str | None) -> bool:
    """Both known and different."""
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and na != nb)


def _tokens(s: str | None) -> set[str]:
    return set(_TOKEN.findall((s or "").lower()))


def variant_similarity(a: str | None, b: str | None) -> float | None:
    """Token overlap of the badges, ignoring drivetrain/fuel/year words sites add."""
    ta, tb = _tokens(variant_core(a)), _tokens(variant_core(b))
    if not ta or not tb:
        return None
    return len(ta & tb) / len(ta | tb)


def compare(a: Car, b: Car, cfg: MatchConfig = MatchConfig()) -> Match | None:
    va, vb = clean_vin(a.vin), clean_vin(b.vin)
    if va and vb:
        return Match("vin", 1.0) if va == vb else None
    ra, rb = clean_rego(a.rego), clean_rego(b.rego)
    if ra and rb and ra == rb and not _conflict(a.state, b.state) and not _conflict(a.make, b.make):
        return Match("rego", 1.0)

    if a.source == b.source:
        return None
    if a.odometer_km is None or b.odometer_km is None:
        return None
    if not (a.make and b.make and a.model and b.model):
        return None
    for x, y in ((a.make, b.make), (a.model, b.model), (a.state, b.state), (a.body_type, b.body_type),
                 (a.transmission, b.transmission), (a.fuel_type, b.fuel_type),
                 (drivetrain_family(a.drivetrain), drivetrain_family(b.drivetrain)), (a.colour, b.colour)):
        if _conflict(x, y):
            return None

    w = cfg.weights
    km_tol = max(cfg.km_abs, cfg.km_pct * max(a.odometer_km, b.odometer_km))
    km_diff = abs(a.odometer_km - b.odometer_km)
    if km_diff > km_tol:
        return None
    score = w["km"] * (1 - km_diff / km_tol)

    if a.year is not None and b.year is not None:
        dy = abs(a.year - b.year)
        if dy > cfg.year_tolerance:
            return None
        score += w["year"] * (1.0 if dy == 0 else 0.5)
    else:
        score += w["year"] * 0.5  # unknown: neither evidence for nor against

    if a.price and b.price:
        dp = abs(a.price - b.price) / max(a.price, b.price)
        if dp > cfg.price_pct:
            return None
        score += w["price"] * (1 - dp / cfg.price_pct)
    else:
        score += w["price"] * 0.5

    sim = variant_similarity(a.variant, b.variant)
    score += w["variant"] * (0.5 if sim is None else sim)

    return Match("fuzzy", round(score, 3)) if score >= cfg.threshold else None


@dataclass
class Cluster:
    cars: list[Car]
    method: str = "single"
    confidence: float = 1.0

    @property
    def vin(self) -> str | None:
        return next((v for c in self.cars if (v := clean_vin(c.vin))), None)

    @property
    def rego(self) -> str | None:
        return next((r for c in self.cars if (r := clean_rego(c.rego))), None)

    def canonical(self) -> Car:
        """The listing to show for the car: active first, then the most complete, then freshest."""
        def key(c: Car) -> tuple:
            filled = sum(v is not None for v in (c.vin, c.variant, c.year, c.odometer_km, c.price,
                                                 c.state, c.body_type, c.transmission, c.colour))
            seen = c.last_seen.timestamp() if c.last_seen else 0.0
            return (c.is_active, filled, seen, -c.id)
        return max(self.cars, key=key)


def candidate_pairs(cars: list[Car], cfg: MatchConfig) -> set[tuple[int, int]]:
    """Pairs worth comparing, without trying all n^2: same VIN or rego, plus neighbours in
    odometer order (a fuzzy match needs the readings within tolerance anyway)."""
    out: set[tuple[int, int]] = set()
    for key in (lambda c: clean_vin(c.vin), lambda c: clean_rego(c.rego)):
        index: dict[str, list[int]] = {}
        for i, c in enumerate(cars):
            if (k := key(c)):
                index.setdefault(k, []).append(i)
        for idx in index.values():
            out.update((min(x, y), max(x, y)) for n, x in enumerate(idx) for y in idx[n + 1:])
    by_km = sorted((c.odometer_km, i) for i, c in enumerate(cars) if c.odometer_km is not None)
    for n, (km_i, i) in enumerate(by_km):
        for km_j, j in by_km[n + 1:]:
            if km_j - km_i > max(cfg.km_abs, cfg.km_pct * km_j):
                break
            out.add((min(i, j), max(i, j)))
    return out


def cluster(cars: list[Car], cfg: MatchConfig = MatchConfig()) -> list[Cluster]:
    """Group listings into cars. Complete linkage: two groups merge only when *every* pair across
    them matches, so A~B and B~C never drags in a C that doesn't look like A. Strongest pairs merge
    first. Expects cars already blocked by make/model (comparisons are pairwise)."""
    pairs: list[tuple[float, int, int, Match]] = []
    for i, j in candidate_pairs(cars, cfg):
        m = compare(cars[i], cars[j], cfg)
        if m:
            pairs.append((METHOD_RANK[m.method] + m.score, i, j, m))
    matches = {(i, j): m for _, i, j, m in pairs}
    pairs.sort(key=lambda p: -p[0])

    group = list(range(len(cars)))                    # car index -> group id
    members: dict[int, list[int]] = {i: [i] for i in range(len(cars))}
    for _, i, j, _m in pairs:
        gi, gj = group[i], group[j]
        if gi == gj:
            continue
        if not all((min(x, y), max(x, y)) in matches for x in members[gi] for y in members[gj]):
            continue
        for x in members[gj]:
            group[x] = gi
        members[gi] += members.pop(gj)

    out = []
    for idx in members.values():
        if len(idx) == 1:
            out.append(Cluster([cars[idx[0]]]))
            continue
        # Every pair inside matched (complete linkage); the cluster is as strong as its weakest pair.
        links = [matches[(min(x, y), max(x, y))] for k, x in enumerate(idx) for y in idx[k + 1:]]
        weakest = min(links, key=lambda m: (METHOD_RANK[m.method], m.score))
        out.append(Cluster([cars[i] for i in idx], weakest.method, weakest.score))
    return out
