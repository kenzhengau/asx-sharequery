"""Async client for the public ASX research API (Markit Digital).

The endpoints used here are the same JSON services that power the company pages on
``asx.com.au``. They are public and need no API key, but they are undocumented and can
change without notice, so every response is parsed defensively.

Known limitation: the announcements endpoint ignores paging parameters and always
returns the five most recent announcements for a code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

API_BASE = "https://asx.api.markitdigital.com/asx-research/1.0"
COMPANY_PAGE_BASE = "https://www.asx.com.au/markets/company"

# The ASX API caps this endpoint at the five most recent announcements.
MAX_ANNOUNCEMENTS = 5

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}$")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.asx.com.au/",
}

# Best-effort subset of ASX trading status codes. Anything not listed is passed
# through to the model verbatim rather than guessed at.
_STATUS_CODES = {
    "": "Normal trading",
    "XD": "Ex-dividend",
    "XDC": "Ex-dividend (with conduit foreign income)",
    "XR": "Ex-rights",
    "XA": "Ex-all entitlements",
    "XT": "Ex-entitlement",
    "H": "Trading halt",
    "S": "Suspended from quotation",
    "P": "Trading paused",
}


class AsxError(RuntimeError):
    """Raised when an ASX lookup fails."""


class UnknownSymbolError(AsxError):
    """Raised when the ASX does not recognise the requested code."""


def normalize_symbol(raw: str) -> str:
    """Normalise user input such as ``" bhp "`` or ``"bhp.ax"`` into ``"BHP"``.

    Args:
        raw: The ticker as typed by the user.

    Returns:
        The upper-cased ASX code without a Yahoo-style ``.AX`` suffix.

    Raises:
        UnknownSymbolError: if the value cannot be a plausible ASX code.
    """
    symbol = (raw or "").strip().upper()
    if symbol.endswith(".AX"):
        symbol = symbol[:-3]

    if not _SYMBOL_RE.match(symbol):
        raise UnknownSymbolError(
            f"'{raw}' does not look like an ASX code. "
            "Codes are 1-6 letters/digits, for example BHP, CBA, CSL, WES, A2M."
        )
    return symbol


def company_page_url(symbol: str) -> str:
    """Return the public ASX company page for a code.

    This is the human-readable page where a user can open the announcements the tools
    only return headlines for.
    """
    return f"{COMPANY_PAGE_BASE}/{normalize_symbol(symbol)}"


def _error_message(payload: Any) -> str | None:
    """Extract the human-readable message from an ASX error body, if there is one."""
    if not isinstance(payload, dict):
        return None

    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("reason")
        if message:
            return str(message).removeprefix("Bad Request: ")
    return None


@dataclass(frozen=True)
class Announcement:
    """A single ASX company announcement.

    ``document_url`` is taken from the feed's own ``url`` field and is usually empty:
    the ASX does not publish a stable deep link for announcement documents, so callers
    should fall back to :func:`company_page_url` rather than constructing one.
    """

    headline: str
    published: datetime | None
    announcement_type: str
    price_sensitive: bool
    file_size: str | None = None
    document_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "headline": self.headline,
            "published": self.published.isoformat() if self.published else None,
            "published_local": (
                self.published.astimezone().strftime("%Y-%m-%d %H:%M %Z")
                if self.published
                else None
            ),
            "type": self.announcement_type,
            "price_sensitive": self.price_sensitive,
            "document_size": self.file_size,
            "document_url": self.document_url,
        }


@dataclass(frozen=True)
class ShareSnapshot:
    """A point-in-time view of a listed security."""

    symbol: str
    name: str
    last_price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    market_cap_aud: int | None = None
    sector: str | None = None
    industry_group: str | None = None
    date_listed: str | None = None
    status_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status_description(self) -> str | None:
        """Human-readable meaning of the ASX trading status code."""
        if self.status_code is None:
            return None
        return _STATUS_CODES.get(self.status_code, f"ASX status code '{self.status_code}'")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "currency": "AUD",
            "last_price": self.last_price,
            "change": self.change,
            "change_percent": (
                round(self.change_percent, 2) if self.change_percent is not None else None
            ),
            "bid": self.bid,
            "ask": self.ask,
            "volume": self.volume,
            "market_cap_aud": self.market_cap_aud,
            "sector": self.sector,
            "industry_group": self.industry_group,
            "date_listed": self.date_listed,
            "trading_status": self.status_description,
        }


class AsxClient:
    """Thin async wrapper around the ASX research endpoints.

    Use as an async context manager so the underlying connection pool is closed::

        async with AsxClient() as client:
            await client.get_snapshot("BHP")
    """

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self) -> AsxClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def _get(self, path: str) -> Any:
        """GET ``path`` and return the unwrapped ``data`` object."""
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:  # network, DNS, timeout
            raise AsxError(f"could not reach the ASX service ({exc.__class__.__name__})") from exc

        try:
            payload: Any = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            message = _error_message(payload) or f"HTTP {response.status_code}"
            # Note: the ASX reports an unknown code as HTTP 400 ("Symbol not found"),
            # not 404, so both statuses are treated as "no such code".
            if response.status_code == 404 or "not found" in message.lower():
                raise UnknownSymbolError(f"the ASX does not list that code ({message})")
            raise AsxError(f"the ASX service returned HTTP {response.status_code} ({message})")

        if payload is None:
            raise AsxError("the ASX service returned a non-JSON response")

        if isinstance(payload, dict) and payload.get("error"):
            raise AsxError(_error_message(payload) or "the ASX service returned an error")

        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            raise UnknownSymbolError("the ASX has no data for that code")
        return data

    async def get_snapshot(self, symbol: str) -> ShareSnapshot:
        """Fetch the company header: name, price, change, volume and sector.

        Args:
            symbol: An ASX code such as ``BHP``.

        Returns:
            The parsed snapshot.

        Raises:
            UnknownSymbolError: if the code is not listed.
            AsxError: if the service cannot be reached or returns unexpected data.
        """
        code = normalize_symbol(symbol)
        data = await self._get(f"/companies/{code}/header")

        def _num(key: str) -> float | None:
            value = data.get(key)
            return value if isinstance(value, (int, float)) else None

        return ShareSnapshot(
            symbol=str(data.get("symbol") or code),
            name=str(data.get("displayName") or code),
            last_price=_num("priceLast"),
            change=_num("priceChange"),
            change_percent=_num("priceChangePercent"),
            bid=_num("priceBid"),
            ask=_num("priceAsk"),
            volume=int(v) if (v := _num("volume")) is not None else None,
            market_cap_aud=(
                int(m) if (m := _num("marketCap")) is not None else None
            ),
            sector=data.get("sector"),
            industry_group=data.get("industryGroup"),
            date_listed=data.get("dateListed"),
            status_code=data.get("statusCode"),
        )

    async def get_announcements(self, symbol: str) -> list[Announcement]:
        """Fetch the most recent announcements for a code (ASX caps this at five).

        Args:
            symbol: An ASX code such as ``BHP``.

        Returns:
            Announcements, newest first.

        Raises:
            UnknownSymbolError: if the code is not listed.
            AsxError: if the service cannot be reached or returns unexpected data.
        """
        code = normalize_symbol(symbol)
        data = await self._get(f"/companies/{code}/announcements")

        items = data.get("items")
        if not isinstance(items, list):
            raise AsxError("the ASX announcement response had an unexpected shape")

        announcements: list[Announcement] = []
        for item in items[:MAX_ANNOUNCEMENTS]:
            if not isinstance(item, dict):
                continue

            published: datetime | None = None
            raw_date = item.get("date")
            if isinstance(raw_date, str):
                try:
                    published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    published = None

            raw_url = item.get("url")
            announcements.append(
                Announcement(
                    headline=str(item.get("headline") or "Untitled announcement"),
                    published=published,
                    announcement_type=str(item.get("announcementType") or "OTHER"),
                    price_sensitive=bool(item.get("isPriceSensitive")),
                    file_size=item.get("fileSize") or None,
                    document_url=raw_url if isinstance(raw_url, str) and raw_url else None,
                )
            )

        return announcements
