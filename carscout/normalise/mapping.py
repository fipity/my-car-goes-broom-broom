"""Name and spec normalisation. Mapping tables live in normalise/data/*.yaml; extend them there.

- makes.yaml / models.yaml: make and model aliases ("VW" -> "Volkswagen", "hi-lux" -> "HiLux")
- specs.yaml: body type, transmission, fuel and drivetrain vocabularies
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


@lru_cache
def _make_table() -> dict[str, str]:
    raw = yaml.safe_load((DATA / "makes.yaml").read_text()) or {}
    table: dict[str, str] = {}
    for canonical, aliases in raw.items():
        table[_key(canonical)] = canonical
        for alias in aliases or []:
            table[_key(str(alias))] = canonical
    return table


@lru_cache
def _model_table() -> dict[str, dict[str, str]]:
    raw = yaml.safe_load((DATA / "models.yaml").read_text()) or {}
    out: dict[str, dict[str, str]] = {}
    for make, models in raw.items():
        if str(make).startswith("_"):
            continue
        t: dict[str, str] = {}
        for canonical, aliases in (models or {}).items():
            canonical = str(canonical)
            t[_key(canonical)] = canonical
            for alias in aliases or []:
                t[_key(str(alias))] = canonical
        out[make] = t
    return out


def _smart_title(s: str) -> str:
    # Tokens with digits or in all caps (i30, CX-5, GTI, BMW) stay as written; words get Title case.
    return " ".join(
        w if any(c.isdigit() for c in w) or w.isupper() else w[:1].upper() + w[1:].lower()
        for w in s.split()
    )


def normalise_make(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    return _make_table().get(_key(name), _smart_title(name.strip()))


@lru_cache
def _model_rules() -> dict[str, list[tuple[re.Pattern[str], str, str | None]]]:
    raw = (yaml.safe_load((DATA / "models.yaml").read_text()) or {}).get("_rules") or {}
    return {
        make: [(re.compile(r["pattern"], re.I), r["model"], r.get("variant_prefix")) for r in rules or []]
        for make, rules in raw.items()
    }


def _fill(template: str, m: re.Match[str]) -> str:
    def group(g: re.Match[str]) -> str:
        v = (m.group(int(g.group(1))) or "").replace(" ", "")
        return v.upper() if v.isalpha() and len(v) <= 3 else v  # class letters: "c" -> "C"

    return re.sub(r"\{(\d+)\}", group, template)


_BADGE = re.compile(r"^(?:[A-Za-z]{2}|[B-DF-HJ-NP-TV-Zb-df-hj-np-tv-z]{3}|[A-Za-z]+\d+[A-Za-z]*)$")


def normalise_variant(variant: str | None) -> str | None:
    """Badge capitalisation: "Zr Hybrid" -> "ZR Hybrid", "Gxl" -> "GXL", "sr5" -> "SR5".
    Two-letter tokens, vowel-less three-letter tokens and letter+digit tokens are badges."""
    if not variant or not variant.strip():
        return None
    return " ".join(w.upper() if _BADGE.match(w) else w for w in variant.split())


def normalise_model_variant(
    make: str | None, model: str | None, variant: str | None = None
) -> tuple[str | None, str | None]:
    """Model plus variant, applying the pattern rules in models.yaml (e.g. Mercedes "C200" ->
    model "C-Class", variant "C200 <variant>")."""
    variant = normalise_variant(variant)
    if not model or not model.strip():
        return None, variant
    model = model.strip()
    table = _model_table().get(make or "", {})
    if _key(model) in table:
        return table[_key(model)], variant
    for rx, model_t, prefix_t in _model_rules().get(make or "", []):
        m = rx.match(model)
        if m:
            new_model = _fill(model_t, m)
            if prefix_t:
                prefix = _fill(prefix_t, m)
                if not variant or not variant.upper().startswith(prefix):
                    variant = f"{prefix} {variant}" if variant else prefix
            return new_model, variant
    return _smart_title(model), variant


def normalise_model(make: str | None, model: str | None) -> str | None:
    return normalise_model_variant(make, model)[0]


_STATES = {"vic": "VIC", "victoria": "VIC", "nsw": "NSW", "newsouthwales": "NSW", "qld": "QLD",
           "queensland": "QLD", "sa": "SA", "southaustralia": "SA", "wa": "WA",
           "westernaustralia": "WA", "tas": "TAS", "tasmania": "TAS", "act": "ACT",
           "australiancapitalterritory": "ACT", "nt": "NT", "northernterritory": "NT"}


def normalise_state(s: str | None) -> str | None:
    if not s:
        return None
    return _STATES.get(_key(s))


@lru_cache
def _spec_tables() -> dict[str, dict[str, str]]:
    raw = yaml.safe_load((DATA / "specs.yaml").read_text()) or {}
    out: dict[str, dict[str, str]] = {}
    for fld, values in raw.items():
        t: dict[str, str] = {}
        for canonical, aliases in (values or {}).items():
            canonical = str(canonical)
            t[_key(canonical)] = canonical
            for alias in aliases or []:
                t[_key(str(alias))] = canonical
        out[fld] = t
    return out


def normalise_spec(field: str, value: str | None) -> str | None:
    """field is one of body_type, transmission, fuel_type, drivetrain."""
    if not value or not str(value).strip():
        return None
    value = str(value).strip()
    return _spec_tables().get(field, {}).get(_key(value), _smart_title(value))


_DRIVE_FAMILY = {"fwd": "2wd", "rwd": "2wd", "2wd": "2wd", "awd": "awd", "4wd": "awd"}


def drivetrain_family(value: str | None) -> str | None:
    """2wd | awd | None. Sites disagree on FWD vs "2WD" and AWD vs "4x4" for the same car,
    so comparisons use the family."""
    return _DRIVE_FAMILY.get(_key(value or ""))


# Words sites put in the variant that are really drivetrain, fuel, transmission or model year.
_NOT_BADGE = re.compile(r"\b(?:hybrid|hybr|petrol|diesel|electric|phev|2wd|awd|4wd|4x4|4x2|fwd|rwd|"
                        r"auto|automatic|manual|cvt|my\d{2}|full size spare|e-?four)\b", re.I)


def variant_core(variant: str | None) -> str | None:
    """The badge alone, for comparing variants across sites: "GX (2WD) Hybrid" -> "gx"."""
    if not variant:
        return None
    core = _NOT_BADGE.sub(" ", variant)
    core = re.sub(r"[()\[\],]", " ", core)
    core = " ".join(core.lower().split())
    return core or None


def same_name(a: str | None, b: str | None) -> bool:
    """Loose equality for normalised names: ignores case, spaces and punctuation."""
    return a is not None and b is not None and _key(a) == _key(b)
