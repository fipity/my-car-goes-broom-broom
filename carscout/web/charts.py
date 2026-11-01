"""Server-rendered price history chart (inline SVG, no JavaScript).

One step line per source: a listing's price stays flat until it changes. Each source keeps the
same colour everywhere (colour follows the source, not its rank): the --series-N tokens in
app.css, validated for both themes with the dataviz palette checks. Three slots at most; a fourth
source would need a new validated slot, not a generated hue. The table under the chart is the
accessible view of the same data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from carscout.core.config import load_sources
from carscout.models import Listing, PriceHistory

# Small canvas, so 12-unit text stays readable when a phone shrinks it (CSS caps the width).
W, H = 420, 190
PAD_L, PAD_R, PAD_T, PAD_B = 56, 62, 12, 26


@dataclass
class Point:
    x: float
    y: float
    price: int
    when: datetime


@dataclass
class Series:
    source: str
    label: str
    slot: int
    path: str
    points: list[Point]
    end_x: float
    end_y: float
    end_price: int
    active: bool
    label_y: float = 0.0  # end label position, nudged apart from other series' labels
    show_label: bool = True


@dataclass
class Chart:
    width: int = W
    height: int = H
    series: list[Series] = field(default_factory=list)
    y_ticks: list[tuple[float, int]] = field(default_factory=list)   # (y, price)
    x_ticks: list[tuple[float, datetime]] = field(default_factory=list)
    plot: tuple[float, float, float, float] = (PAD_L, PAD_T, W - PAD_R, H - PAD_B)


def source_slot(source: str) -> int:
    """Fixed colour slot per source, from the order of working sources in sources.yaml."""
    names = [n for n, c in load_sources().items() if c.implemented and c.enabled]
    return min(names.index(source) + 1, 3) if source in names else 3


def _nice_step(span: float) -> int:
    raw = span / 3
    mag = 10 ** math.floor(math.log10(max(raw, 1)))
    return int(min((s * mag for s in (1, 2, 2.5, 5, 10)), key=lambda s: abs(s - raw)))


def price_chart(history: list[tuple[PriceHistory, str]], listings: list[Listing],
                now: datetime | None = None) -> Chart | None:
    """history: (price point, source) for every listing of the car. None unless some listing's
    price has actually changed: flat stubs say nothing the table doesn't."""
    now = now or datetime.now(UTC)
    by_source: dict[str, list[PriceHistory]] = {}
    for p, src in sorted(history, key=lambda r: r[0].observed_at):
        if p.price is not None:
            by_source.setdefault(src, []).append(p)
    if not any(len(ps) >= 2 for ps in by_source.values()):
        return None

    ends: dict[str, tuple[datetime, bool]] = {}
    for l in listings:
        end = now if l.is_active else l.last_seen
        prev = ends.get(l.source)
        if prev is None or end > prev[0]:
            ends[l.source] = (end, l.is_active)

    t0 = min(ps[0].observed_at for ps in by_source.values())
    t1 = max([e for e, _ in ends.values()] + [ps[-1].observed_at for ps in by_source.values()])
    if (t1 - t0) < timedelta(hours=12):
        t1 = t0 + timedelta(hours=12)
    prices = [p.price for ps in by_source.values() for p in ps]
    step = _nice_step(max(max(prices) - min(prices), max(prices) * 0.06))
    lo = math.floor(min(prices) / step) * step
    hi = math.ceil(max(prices) / step) * step
    if hi == lo:
        hi = lo + step

    x0, y0, x1, y1 = PAD_L, PAD_T, W - PAD_R, H - PAD_B

    def X(t: datetime) -> float:
        return round(x0 + (t - t0) / (t1 - t0) * (x1 - x0), 1)

    def Y(v: float) -> float:
        return round(y1 - (v - lo) / (hi - lo) * (y1 - y0), 1)

    chart = Chart()
    chart.y_ticks = [(Y(v), v) for v in range(int(lo), int(hi) + 1, step)]
    chart.x_ticks = [(X(t0), t0), (X(t1), t1)]
    labels = {n: c.label for n, c in load_sources().items()}
    for src, ps in sorted(by_source.items(), key=lambda kv: source_slot(kv[0])):
        pts = [Point(X(p.observed_at), Y(p.price), p.price, p.observed_at) for p in ps]
        end_t, active = ends.get(src, (ps[-1].observed_at, False))
        end_x = max(X(end_t), pts[-1].x)
        d = f"M{pts[0].x},{pts[0].y}"
        for a, b in zip(pts, pts[1:]):
            d += f" H{b.x} V{b.y}"
        d += f" H{end_x}"
        chart.series.append(Series(src, labels.get(src, src), source_slot(src), d, pts,
                                   end_x, pts[-1].y, pts[-1].price, active, label_y=pts[-1].y))
    # One end label per price; keep different labels apart when they'd overlap.
    shown: set[int] = set()
    for sr in sorted(chart.series, key=lambda s: s.label_y):
        sr.show_label = sr.end_price not in shown
        shown.add(sr.end_price)
    labelled = [sr for sr in sorted(chart.series, key=lambda s: s.label_y) if sr.show_label]
    for a, b in zip(labelled, labelled[1:]):
        if b.label_y - a.label_y < 15:
            b.label_y = a.label_y + 15
    return chart
