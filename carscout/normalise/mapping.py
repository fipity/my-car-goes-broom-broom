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


def normalise_model(make: str | None, model: str | None) -> str | None:
    if not model or not model.strip():
        return None
    table = _model_table().get(make or "", {})
    return table.get(_key(model), _smart_title(model.strip()))


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


def same_name(a: str | None, b: str | None) -> bool:
    """Loose equality for normalised names: ignores case, spaces and punctuation."""
    return a is not None and b is not None and _key(a) == _key(b)
