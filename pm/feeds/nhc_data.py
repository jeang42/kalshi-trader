"""NHC (National Hurricane Center) active-storm feed.

Built 2026-08-24 as the primary-source half of the storm-detection trigger
(`pm/research/storm_watch.py`) — the sole surviving edge candidate after the
2026-08-24 search is scheduled-advisory weather, and its instrument gets
specified only once a named storm exists. This module answers "does one
exist" from the source of truth rather than from Kalshi's listings.

``CurrentStorms.json`` is NHC's machine-readable list of active tropical
cyclones, updated with the advisory cycle (5/11 AM/PM ET). It is also the
natural seed for the eventual advisory instrument, which is why this lives in
``pm/feeds`` rather than inside the watcher.

Handled defensively like the other unofficial-ish feeds (`underlying_data`):
the schema is NHC's to change, every access is ``.get``-guarded, and
transport failures return [] rather than raise. Under the ``nhc`` circuit
breaker per R-21.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

NHC_BASE = os.getenv("NHC_BASE", "https://www.nhc.noaa.gov")
HTTP_TIMEOUT = float(os.getenv("NHC_HTTP_TIMEOUT", "20"))

#: classifications that count as a NAMED storm (the trigger condition).
#: TD (tropical depression) and PTC (potential t.c.) are deliberately out —
#: they have numbers, not names, and Kalshi lists nothing for them.
NAMED_CLASSIFICATIONS = {"TS", "HU", "MH", "STS", "SS"}


@dataclass(frozen=True)
class Storm:
    """One active tropical cyclone from CurrentStorms.json."""

    id: str              # e.g. "al052026" — stable per storm, the dedupe key
    name: str            # e.g. "Erin"
    classification: str  # TD / TS / HU / MH / SS / STS / PTC
    basin: str           # "AL" (Atlantic) / "EP" / "CP" derived from id

    @property
    def is_named(self) -> bool:
        return self.classification in NAMED_CLASSIFICATIONS


def _fetch_unguarded() -> dict | None:
    r = requests.get(f"{NHC_BASE}/CurrentStorms.json", timeout=HTTP_TIMEOUT,
                     headers={"User-Agent": "polymarket-analyzer storm-watch"})
    r.raise_for_status()
    return r.json()


def active_storms() -> list[Storm]:
    """Every active cyclone NHC is tracking, [] on any failure."""
    try:
        data = circuit_breaker.get("nhc").call(_fetch_unguarded)
    except circuit_breaker.CircuitOpenError as e:
        log.warning("NHC: %s", e)
        return []
    except requests.RequestException as e:
        log.warning("NHC fetch failed: %s", e)
        return []
    return parse_current_storms(data)


def parse_current_storms(data: dict | None) -> list[Storm]:
    """Pure parser, defensively shaped.

    >>> parse_current_storms({"activeStorms": [
    ...     {"id": "al052026", "name": "Erin", "classification": "HU"},
    ...     {"id": "ep092026", "name": "Nine", "classification": "TD"}]})
    ... # doctest: +NORMALIZE_WHITESPACE
    [Storm(id='al052026', name='Erin', classification='HU', basin='AL'),
     Storm(id='ep092026', name='Nine', classification='TD', basin='EP')]
    >>> parse_current_storms({"activeStorms": []})
    []
    >>> parse_current_storms(None)
    []
    >>> parse_current_storms({"unexpected": "shape"})
    []
    """
    if not isinstance(data, dict):
        return []
    out = []
    for s in data.get("activeStorms") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip().lower()
        name = str(s.get("name") or "").strip()
        cls = str(s.get("classification") or "").strip().upper()
        if not sid or not name:
            continue
        out.append(Storm(id=sid, name=name, classification=cls,
                         basin=sid[:2].upper() if len(sid) >= 2 else "?"))
    return out
