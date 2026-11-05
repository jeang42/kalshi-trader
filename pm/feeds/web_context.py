"""Web-fetched context for Deep Analysis (Phase B).

Two free sources — Google News RSS for recent headlines, Wikipedia
REST API for entity summaries. Both unauthenticated. Both cached in
the existing ``llm_cache`` SQLite store keyed on the query/title so
repeated runs on the same market within the TTL window don't re-hit
the network.

Honesty rule for the consuming LLM: the Deep Analysis prompt asks
for an ``evidence_used`` list. When web context is supplied, that
list must cite specific headlines or Wikipedia paragraphs. If the
model returns analysis without citing the fetched content, treat
the result as ungrounded — same standard as Phase A.

No paid APIs. No keys. Single-user rate-limit assumptions; if we
ever go multi-user this needs reconsidering.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

import requests

from pm.core import circuit_breaker
from pm.feeds import llm_cache

log = logging.getLogger(__name__)

# Google News RSS endpoint (no auth, no rate limits at our scale).
# Locale params bias toward English-language US sources, which is
# what most political/macro markets we trade on are about.
_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Wikipedia REST API — page summary endpoint returns plaintext +
# extract for a given page title.
_WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"

# Wikipedia search API for resolving "did this entity have a page?"
_WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/api.php"

# Default cache TTLs. News drifts fastest; Wikipedia is stable.
_NEWS_TTL_SECONDS = 6 * 3600    # 6h
_WIKI_TTL_SECONDS = 7 * 86400   # 7 days

_HTTP_TIMEOUT = 10.0

# Wikipedia's UA policy requires identifying contact info — they
# 403 a bare GitHub URL. Format: ``<app>/<ver> (<email>)``. The
# email is purely a contact hop; nobody actually emails us.
_USER_AGENT = "kalshi-trader/0.2 (+https://github.com/jeang42/kalshi-trader)"

_session = requests.Session()
_session.headers.update({
    "User-Agent": _USER_AGENT,
    "Accept": "application/json, application/rss+xml, text/xml",
})


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str   # e.g. "Reuters", "AP"
    published: str  # RFC822 string as-is from the feed
    url: str


@dataclass(frozen=True)
class WikiSummary:
    title: str
    extract: str       # plain-text summary
    url: str


# ---------------------------------------------------------------------------
# Google News RSS
# ---------------------------------------------------------------------------

def search_news(query: str, *, max_items: int = 5,
                 bypass_cache: bool = False,
                 cache_ttl_seconds: int = _NEWS_TTL_SECONDS) -> list[NewsItem]:
    """Fetch recent news headlines for ``query`` via Google News RSS.

    Returns an empty list on transport failure or empty feed — never
    raises. Cached for ``cache_ttl_seconds`` (6h default).
    """
    q = (query or "").strip()
    if not q:
        return []

    cache_key = llm_cache.make_key("web_news/v1", q, str(max_items))
    if not bypass_cache:
        cached = llm_cache.get(cache_key)
        if cached is not None:
            try:
                return [NewsItem(**item) for item in cached.get("items", [])]
            except TypeError:
                log.warning("web_news: cache schema mismatch — refetching")

    params = {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}

    def _fetch() -> requests.Response:
        resp = _session.get(_GOOGLE_NEWS_RSS, params=params,
                             timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp

    try:
        r = circuit_breaker.get("google_news").call(_fetch)   # R-21
    except requests.RequestException as e:
        log.warning("web_news search %r failed: %s", q, e)
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.warning("web_news: bad XML for %r: %s", q, e)
        return []

    items: list[NewsItem] = []
    for entry in root.iter("item"):
        if len(items) >= max_items:
            break
        title = (entry.findtext("title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        pub = (entry.findtext("pubDate") or "").strip()
        source_el = entry.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        if title and link:
            items.append(NewsItem(
                title=title, source=source, published=pub, url=link,
            ))

    # Persist serializable form (dicts) so reloading via cache works.
    llm_cache.set(
        cache_key,
        {"items": [item.__dict__ for item in items]},
        ttl_seconds=cache_ttl_seconds,
    )
    return items


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

def wikipedia_summary(title: str, *,
                       bypass_cache: bool = False,
                       cache_ttl_seconds: int = _WIKI_TTL_SECONDS,
                       ) -> WikiSummary | None:
    """Plain-text summary for a Wikipedia page. Returns None if the
    page doesn't exist, the API errors, or the article is a disambig
    page (we want grounded facts, not a list of candidates).

    The REST summary endpoint handles redirect resolution server-side
    ("Thomas Massie" → article about Thomas Hoover Massie).
    """
    t = (title or "").strip()
    if not t:
        return None

    cache_key = llm_cache.make_key("web_wiki/v1", t)
    if not bypass_cache:
        cached = llm_cache.get(cache_key)
        if cached is not None:
            try:
                return WikiSummary(**cached) if cached.get("title") else None
            except TypeError:
                log.warning("web_wiki: cache schema mismatch — refetching")

    # Wikipedia REST expects %20 for spaces in path segments; quote_plus
    # encodes '+' which the API rejects with 404.
    url = f"{_WIKIPEDIA_REST}/{quote(t)}"
    try:
        r = circuit_breaker.get("wikipedia").call(   # R-21
            _session.get, url, timeout=_HTTP_TIMEOUT)
        if r.status_code == 404:
            # No such page. Cache the negative result so we don't
            # re-query every time.
            llm_cache.set(cache_key, {"title": "", "extract": "", "url": ""},
                           ttl_seconds=cache_ttl_seconds)
            return None
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("wikipedia_summary %r failed: %s", t, e)
        return None
    except ValueError as e:
        log.warning("wikipedia_summary %r returned non-JSON: %s", t, e)
        return None

    # Disambiguation pages have type=="disambiguation"; treat as no-hit.
    if data.get("type") == "disambiguation":
        llm_cache.set(cache_key, {"title": "", "extract": "", "url": ""},
                       ttl_seconds=cache_ttl_seconds)
        return None

    summary = WikiSummary(
        title=data.get("title", "") or t,
        extract=data.get("extract", "") or "",
        url=(data.get("content_urls") or {}).get("desktop", {}).get("page", ""),
    )
    if not summary.extract:
        return None

    llm_cache.set(cache_key, summary.__dict__,
                   ttl_seconds=cache_ttl_seconds)
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_news_for_prompt(items: list[NewsItem], *, max_items: int = 5) -> str:
    """One-line-per-headline string for embedding in an LLM prompt.

    Trims source names and dates to a fixed width so token usage is
    predictable. Returns ``"(no results)"`` on empty input — the model
    should NOT silently treat absence of headlines as evidence.
    """
    if not items:
        return "(no recent headlines found)"
    lines = []
    for it in items[:max_items]:
        src = f" [{it.source}]" if it.source else ""
        lines.append(f"  - {it.title}{src}")
    return "\n".join(lines)


def format_wiki_for_prompt(summary: WikiSummary | None, *,
                            max_chars: int = 1500) -> str:
    """Trim Wikipedia summary to a max char budget for prompt embedding."""
    if summary is None or not summary.extract:
        return "(no Wikipedia page found)"
    text = summary.extract
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return f"  {summary.title}: {text}"


__all__ = [
    "NewsItem",
    "WikiSummary",
    "search_news",
    "wikipedia_summary",
    "format_news_for_prompt",
    "format_wiki_for_prompt",
]
