"""Client for the Google News RSS feed - media coverage about a listed company.

Google News RSS is a public feed that needs no API key. This module deliberately returns
only headlines, source names, timestamps and links - never article text - so the agent can
point a user at the coverage without republishing it.

Note that these are third-party media headlines, not the company's own announcements.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .asx_client import AsxError, normalize_symbol

NEWS_BASE = "https://news.google.com/rss/search"
MAX_ITEMS = 10

_HEADERS = {
    "User-Agent": "asx-sharequery/0.1 (personal research script)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Legal suffixes and filler that say nothing about which company this is.
_STOPWORDS = {"limited", "group", "holdings", "company", "corporation", "australia", "the"}

# Sector words that appear across half the market, so on their own they prove nothing.
_GENERIC_WORDS = {
    "resources",
    "minerals",
    "mining",
    "energy",
    "gold",
    "metals",
    "iron",
    "coal",
    "silver",
    "lithium",
    "bank",
    "banks",
    "insurance",
    "healthcare",
    "telecom",
    "retail",
    "property",
    "investments",
    "capital",
    "financial",
    "technology",
    "pharma",
    "biotech",
    "gas",
    "oil",
    "steel",
    "cement",
}

_NAME_WORD_RE = re.compile(r"[A-Za-z]{4,}")

# Only the explicit "ASX:BHP" / "(ASX: BHP)" form. Matching a bare "ASX" would read the
# common phrase "ASX shares" as a ticker called SHARES and throw away real news.
_ASX_TICKER_RE = re.compile(r"\bASX:\s*([A-Z][A-Z0-9]{1,5})\b", re.IGNORECASE)

# Sort placeholder for undated items; always timezone-aware so comparisons never mix
# aware and naive datetimes.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class NewsError(RuntimeError):
    """Raised when the news feed cannot be read."""


@dataclass(frozen=True)
class NewsItem:
    """One media headline about a company."""

    headline: str
    source: str | None
    published: datetime | None
    url: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "headline": self.headline,
            "source": self.source,
            "published": self.published.isoformat() if self.published else None,
            "published_local": (
                self.published.astimezone().strftime("%Y-%m-%d %H:%M %Z")
                if self.published
                else None
            ),
            "url": self.url,
        }


def build_query(symbol: str, company_name: str | None = None) -> str:
    """Build the news search query.

    Pinning the query to ``ASX`` is what keeps results on the Australian listing rather
    than the many unrelated things three-letter tickers also mean.
    """
    if company_name:
        return f"{company_name} ASX"
    return f"{symbol} ASX"


def search_url(query: str) -> str:
    """One Google News link a human can open to browse the coverage themselves.

    Per-article links are deliberately not offered: each is a ``news.google.com``
    redirect hundreds of characters long, and outside a browser it resolves to an
    interstitial rather than the publisher.
    """
    return f"https://news.google.com/search?q={quote(query)}&hl=en-AU&gl=AU&ceid=AU:en"


def _clean_headline(raw: str, source: str | None) -> str:
    """Google appends " - Source" to each title; drop it when it matches."""
    if source and raw.endswith(f" - {source}"):
        return raw[: -(len(source) + 3)].strip()
    return raw.strip()


def _name_words(company_name: str) -> list[str]:
    """The meaningful words of a company name, minus legal suffixes."""
    return [
        word
        for word in _NAME_WORD_RE.findall(company_name)
        if word.lower() not in _STOPWORDS
    ]


def _is_relevant(headline: str, symbol: str, company_name: str | None) -> bool:
    """Decide whether a headline is plausibly about this company.

    Tickers such as ALL, MIN and SUN are ordinary English words, and sector words such as
    "Resources" appear throughout the mining sector, so a naive keyword test happily
    returns confident nonsense. The checks therefore run strongest-first:

    1. the ticker appears as a standalone word - the most reliable signal by far;
    2. no *other* ASX ticker is named, which rules out a different company such as
       "Native Mineral Resources (ASX:NMR)" when you asked about MIN;
    3. enough of the company's own name appears, allowing a single word only when it is
       distinctive ("Wesfarmers") rather than generic ("Resources").
    """
    if re.search(rf"\b{re.escape(symbol)}\b", headline, re.IGNORECASE):
        return True

    mentioned = {code.upper() for code in _ASX_TICKER_RE.findall(headline)}
    if mentioned and symbol.upper() not in mentioned:
        return False

    if not company_name:
        return False

    lowered = headline.lower()
    matched = [word for word in _name_words(company_name) if word.lower() in lowered]

    if len(matched) >= 2:
        return True
    if len(matched) == 1:
        word = matched[0]
        return len(word) >= 5 and word.lower() not in _GENERIC_WORDS
    return False


def _parse_published(raw: str | None) -> datetime | None:
    """Parse an RFC-822 ``pubDate`` value into a timezone-aware datetime."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # a few feeds omit the zone; treat them as UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def get_share_news(
    symbol: str,
    company_name: str | None = None,
    *,
    limit: int = MAX_ITEMS,
    timeout: float = 20.0,
) -> list[NewsItem]:
    """Fetch recent media headlines about a listed company, newest first.

    Args:
        symbol: ASX code used for the query and the relevance check.
        company_name: Optional full company name for a sharper query.
        limit: Maximum number of items to return.
        timeout: HTTP timeout in seconds.

    Returns:
        The matching headlines, newest first.

    Raises:
        NewsError: if the feed cannot be reached or parsed.
    """
    try:
        code = normalize_symbol(symbol)
    except AsxError:
        code = (symbol or "").strip().upper()

    query = build_query(code, company_name)
    url = f"{NEWS_BASE}?q={quote(query)}&hl=en-AU&gl=AU&ceid=AU:en"

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=timeout
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise NewsError(f"could not read the news feed ({exc.__class__.__name__})") from exc

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise NewsError("the news feed returned malformed XML") from exc

    items: list[NewsItem] = []
    for node in root.findall(".//item"):
        raw_title = (node.findtext("title") or "").strip()
        if not raw_title:
            continue

        source_node = node.find("source")
        source = (source_node.text or "").strip() if source_node is not None else None
        headline = _clean_headline(raw_title, source)

        if not _is_relevant(headline, code, company_name):
            continue

        link = (node.findtext("link") or "").strip() or None
        items.append(
            NewsItem(
                headline=headline,
                source=source or None,
                published=_parse_published(node.findtext("pubDate")),
                url=link,
            )
        )

    # Newest first, with undated items last.
    items.sort(key=lambda item: item.published or _EPOCH, reverse=True)
    return items[:limit]
