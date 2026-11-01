"""Pulling data out of Next.js pages without a browser.

- App Router pages stream React Server Components "flight" data via
  `self.__next_f.push([1, "..."])` scripts. Joined together it is a list of rows `id:payload`,
  where JSON payloads reference other rows as "$<hex id>" (and "$$" escapes a literal "$").
- Pages Router / Redux pages embed `__NEXT_DATA__` or `window.__SOMETHING__ = {...}`.
- Both often carry JSON-LD as well.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.S)
_ROW_ID = re.compile(rb"([0-9a-f]+):")
_REF = re.compile(r"\$([0-9a-f]+)")
_LD = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)


def flight_text(html: str) -> str:
    chunks: list[str] = []
    for m in _PUSH.finditer(html):
        try:
            arr = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if len(arr) > 1 and arr[0] == 1 and isinstance(arr[1], str):
            chunks.append(arr[1])
    return "".join(chunks)


def flight_rows(flight: str) -> dict[str, Any]:
    """Parse the flight stream into {row id: decoded JSON}. Text rows (`id:T<hex len>,...`)
    and non-JSON rows (module refs etc.) are skipped. Text row lengths are in UTF-8 bytes."""
    data = flight.encode()
    rows: dict[str, Any] = {}
    pos, n = 0, len(data)
    while pos < n:
        m = _ROW_ID.match(data, pos)
        if not m:  # junk between rows: skip to next line
            nl = data.find(b"\n", pos)
            pos = n if nl == -1 else nl + 1
            continue
        row_id, pos = m.group(1).decode(), m.end()
        if data[pos:pos + 1] == b"T":
            comma = data.find(b",", pos)
            try:
                length = int(data[pos + 1:comma], 16)
            except ValueError:
                length = 0
            pos = comma + 1 + length
            continue
        nl = data.find(b"\n", pos)
        end = n if nl == -1 else nl
        body = data[pos:end]
        pos = end + 1
        if body[:1] in (b"{", b"["):
            try:
                rows[row_id] = json.loads(body)
            except json.JSONDecodeError:
                pass
    return rows


def resolve(value: Any, rows: dict[str, Any], _depth: int = 0) -> Any:
    """Inline "$id" references. Depth-limited, so reference cycles can't loop forever."""
    if _depth > 50:
        return value
    if isinstance(value, str):
        if value.startswith("$$"):
            return value[1:]
        m = _REF.fullmatch(value)
        if m and m.group(1) in rows:
            return resolve(rows[m.group(1)], rows, _depth + 1)
        return value
    if isinstance(value, list):
        return [resolve(v, rows, _depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, rows, _depth + 1) for k, v in value.items()}
    return value


def find_objects(rows: dict[str, Any], *keys: str) -> Iterator[dict[str, Any]]:
    """Resolved dict rows that have all of `keys`, in stream order."""
    for value in rows.values():
        if isinstance(value, dict) and all(k in value for k in keys):
            yield resolve(value, rows)


def assigned_json(html: str, name: str) -> Any | None:
    """The JSON object assigned in `<name> = {...}` (e.g. window.__PRELOADED_STATE__)."""
    i = html.find(name)
    if i == -1:
        return None
    j = html.find("{", i)
    if j == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, j)
    except json.JSONDecodeError:
        return None
    return obj


def json_ld(html: str) -> list[Any]:
    out = []
    for m in _LD.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return out


# ---- Nuxt 3 (__NUXT_DATA__) -----------------------------------------------------------------
_NUXT = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_NUXT_WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}


def nuxt_data(html: str) -> Any | None:
    """Decode Nuxt 3's payload: a flat JSON array where containers hold *indices* into the array,
    and ["Reactive", i]-style markers wrap values. Returns the revived root (or None)."""
    m = _NUXT.search(html)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or not arr:
        return None
    cache: dict[int, Any] = {}

    def revive(i: Any, depth: int = 0) -> Any:
        if not isinstance(i, int) or not 0 <= i < len(arr) or depth > 80:
            return None
        if i in cache:
            return cache[i]
        v = arr[i]
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            cache[i] = out  # before recursing, so cycles terminate
            for k, x in v.items():
                out[k] = revive(x, depth + 1)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in _NUXT_WRAPPERS:
                res = revive(v[1], depth + 1) if len(v) > 1 else None
            elif v and isinstance(v[0], str) and v[0] == "Date":
                res = v[1] if len(v) > 1 else None
            elif v and isinstance(v[0], str) and v[0] in ("Set", "Map"):
                res = [revive(x, depth + 1) for x in v[1:]]
            else:
                res = [revive(x, depth + 1) for x in v]
            cache[i] = res
            return res
        cache[i] = v
        return v

    return revive(0)
