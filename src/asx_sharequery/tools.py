"""Function tools that give the agent read-only access to ASX data.

Each tool returns a JSON string. Tools never raise: on failure they return an
``{"error": ...}`` payload so the model can tell the user what went wrong (or correct
the ticker) instead of the whole run blowing up.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from agent_framework import tool

from .asx_client import (
    MAX_ANNOUNCEMENTS,
    Announcement,
    AsxClient,
    AsxError,
    company_page_url,
)
from .config import ConfigError, SharePointSettings, load_settings
from .news_client import (
    NewsError,
    build_industry_query,
    build_query,
    get_industry_news as fetch_industry_news,
    search_url,
)
from .news_client import get_share_news as fetch_news
from .sharepoint_client import SharePointClient, SharePointError

_SYMBOL_DOC = "The ASX ticker code, for example BHP, CBA, CSL, WES or A2M."

_DEFAULT_MAX_DOCUMENTS = 8
_DEFAULT_CHARS_PER_DOCUMENT = 6000

_SHAREPOINT_SETUP_HINT = (
    "Set SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET, "
    "SHAREPOINT_SITE_URL and SHAREPOINT_LIBRARY in .env. The app registration needs the "
    "'Sites.Read.All' application permission with admin consent granted."
)


def _dump(payload: Any) -> str:
    """Serialise a tool result for the model."""
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _source_url(symbol: str) -> str | None:
    """The ASX company page for a code, or ``None`` if the code is malformed."""
    try:
        return company_page_url(symbol)
    except AsxError:
        return None


def _announcements_payload(symbol: str, announcements: list[Announcement]) -> dict[str, Any]:
    """Shape the announcements list for the model."""
    return {
        "source_url": _source_url(symbol),
        "note": (
            f"The ASX feed exposes only the {MAX_ANNOUNCEMENTS} most recent "
            "announcements per company, and publishes no stable deep link for an "
            "individual announcement - so 'document_url' is usually null. Refer the "
            "user to 'source_url' (the ASX company page) instead, and never invent or "
            "guess a document link."
        ),
        "count": len(announcements),
        "items": [item.to_dict() for item in announcements],
    }


def _failure(symbol: str, exc: Exception) -> str:
    """Build a structured, non-fatal error payload."""
    return _dump(
        {
            "symbol": symbol,
            "error": str(exc),
            "hint": (
                "Ask the user to confirm the ASX code. This tool only works with "
                "codes listed on the Australian Securities Exchange."
            ),
        }
    )


@tool(
    name="get_share_research",
    description=(
        "Get a complete ASX briefing for one listed company: the latest price snapshot "
        "(last price, day change, volume, market cap, sector) plus its most recent "
        "company announcements. Use this first for any question about a specific share. "
        "Returns JSON."
    ),
)
async def get_share_research(symbol: Annotated[str, _SYMBOL_DOC]) -> str:
    """Fetch the price snapshot and recent announcements for an ASX code."""
    try:
        async with AsxClient() as client:
            snapshot, announcements = await asyncio.gather(
                client.get_snapshot(symbol),
                client.get_announcements(symbol),
                return_exceptions=True,
            )
    except AsxError as exc:
        return _failure(symbol, exc)

    if isinstance(snapshot, BaseException) and isinstance(announcements, BaseException):
        return _failure(symbol, snapshot)

    payload: dict[str, Any] = {
        "source": "ASX (asx.com.au)",
        "source_url": _source_url(symbol),
    }

    if isinstance(snapshot, BaseException):
        payload["price"] = {"error": str(snapshot)}
    else:
        payload["price"] = snapshot.to_dict()

    if isinstance(announcements, BaseException):
        payload["announcements"] = {"error": str(announcements)}
    else:
        payload["announcements"] = _announcements_payload(symbol, announcements)

    return _dump(payload)


@tool(
    name="get_share_snapshot",
    description=(
        "Get only the current price snapshot for an ASX-listed company: last price, "
        "day change, bid/ask, volume, market cap, sector and trading status. "
        "Use for price-only questions. Returns JSON."
    ),
)
async def get_share_snapshot(symbol: Annotated[str, _SYMBOL_DOC]) -> str:
    """Fetch the current price snapshot for an ASX code."""
    try:
        async with AsxClient() as client:
            snapshot = await client.get_snapshot(symbol)
    except AsxError as exc:
        return _failure(symbol, exc)
    return _dump(snapshot.to_dict())


@tool(
    name="get_share_announcements",
    description=(
        "Get the most recent ASX company announcements (news, results, dividends, "
        "meetings, director dealings) for a listed company. Returns JSON with "
        "headlines, publication times, price-sensitivity flags and an ASX page link."
    ),
)
async def get_share_announcements(symbol: Annotated[str, _SYMBOL_DOC]) -> str:
    """Fetch the most recent announcements for an ASX code."""
    try:
        async with AsxClient() as client:
            announcements = await client.get_announcements(symbol)
    except AsxError as exc:
        return _failure(symbol, exc)

    return _dump(_announcements_payload(symbol, announcements))


@tool(
    name="get_share_news",
    description=(
        "Get recent media headlines about an ASX-listed company from Google News "
        "(outlets such as The Motley Fool, AFR, Rask and Morningstar). Use this to add "
        "third-party commentary alongside the company's own ASX announcements. Returns "
        "JSON with headlines, outlet names, publication times and links - never article "
        "text. These are media reports, not company disclosures."
    ),
)
async def get_share_news(
    symbol: Annotated[str, _SYMBOL_DOC],
    company_name: Annotated[
        str | None,
        (
            "Optional full company name for a sharper search, for example "
            "'BHP Group Limited'. Pass the exact 'name' value from the price snapshot."
        ),
    ] = None,
) -> str:
    """Fetch recent third-party media headlines about an ASX code."""
    try:
        items = await fetch_news(symbol, company_name)
    except NewsError as exc:
        return _dump(
            {
                "symbol": symbol,
                "error": str(exc),
                "hint": (
                    "The media feed is unavailable right now. Fall back to the ASX "
                    "announcements and say that media coverage could not be retrieved."
                ),
            }
        )

    query = build_query(symbol.upper(), company_name)
    return _dump(
        {
            "source": "Google News (third-party media headlines)",
            "query": query,
            "search_url": search_url(query),
            "count": len(items),
            # Per-article links are Google redirects 250-600 characters long that don't
            # resolve to the publisher outside a browser, so they are withheld and the
            # single search_url is offered instead. Keeping them would make every answer
            # unreadable.
            "items": [
                {key: value for key, value in item.to_dict().items() if key != "url"}
                for item in items
            ],
            "note": (
                "Media headlines only - no article text was read and no per-article link "
                "is available. Give the outlet and the date for each headline, then offer "
                "the single 'search_url' so the user can open the coverage themselves. "
                "These are journalists' reports and opinions, not company disclosures, so "
                "never present one as an ASX announcement or as established fact."
            ),
        }
    )


@tool(
    name="get_industry_news",
    description=(
        "Get broader industry or sector news for an ASX-listed company so the model can "
        "summarise market conditions, peer trends and industry developments that affect "
        "the stock. Returns JSON with headlines, outlet names, publication times and a "
        "search URL for further browsing."
    ),
)
async def get_industry_news(
    symbol: Annotated[str, _SYMBOL_DOC],
    company_name: Annotated[
        str | None,
        "Optional full company name, for example 'BHP Group Limited'.",
    ] = None,
    sector: Annotated[
        str | None,
        "Optional sector or industry label from the company's ASX snapshot, for example 'Materials'.",
    ] = None,
) -> str:
    """Fetch broader sector/industry news around a stock."""
    try:
        items = await fetch_industry_news(symbol, company_name, sector)
    except NewsError as exc:
        return _dump(
            {
                "symbol": symbol,
                "error": str(exc),
                "hint": (
                    "The broader industry news feed is unavailable. Report that the "
                    "company-level coverage was retrieved but the industry context could "
                    "not be fetched."
                ),
            }
        )

    query = build_industry_query(symbol.upper(), company_name, sector)
    return _dump(
        {
            "source": "Google News (industry context)",
            "query": query,
            "search_url": search_url(query),
            "count": len(items),
            "items": [
                {key: value for key, value in item.to_dict().items() if key != "url"}
                for item in items
            ],
            "note": (
                "Broader industry or sector context, not company-specific announcements. "
                "Use this to explain market conditions, peer moves and the wider theme "
                "affecting the stock, then differentiate it clearly from the company's own "
                "ASX disclosures."
            ),
        }
    )


def _sharepoint_settings() -> SharePointSettings:
    """Resolve the SharePoint settings or explain why the feature is unavailable.

    Raises:
        SharePointError: when the integration is unconfigured or only partly configured.
    """
    try:
        settings = load_settings()
    except ConfigError as exc:  # the agent would not have started, but stay safe
        raise SharePointError(str(exc)) from exc

    if settings.sharepoint is None:
        raise SharePointError(settings.sharepoint_error or _SHAREPOINT_SETUP_HINT)
    return settings.sharepoint


@tool(
    name="read_sharepoint_library",
    description=(
        "Read the PDF and Word documents stored in the configured SharePoint document "
        "library and return their text. Use this when the user asks to summarise their "
        "SharePoint library, site documents or files kept there. Returns JSON with the "
        "text of each document, truncated to fit the context window."
    ),
)
async def read_sharepoint_library(
    max_documents: Annotated[
        int, "How many documents to read, newest first. Defaults to 8."
    ] = _DEFAULT_MAX_DOCUMENTS,
    characters_per_document: Annotated[
        int, "Maximum characters of text to return per document. Defaults to 6000."
    ] = _DEFAULT_CHARS_PER_DOCUMENT,
) -> str:
    """Read the documents in the configured SharePoint library."""
    try:
        settings = _sharepoint_settings()
    except SharePointError as exc:
        return _dump(
            {
                "error": str(exc),
                "hint": (
                    "SharePoint is not available. Tell the user how to configure it "
                    "rather than guessing at the documents' contents."
                ),
            }
        )

    max_documents = max(1, min(int(max_documents), 25))
    characters_per_document = max(500, min(int(characters_per_document), 20000))

    try:
        async with SharePointClient(settings) as client:
            site = await client.resolve_site()
            drive = await client.resolve_drive(site["id"])
            files = await client.list_files(drive["id"], max_files=max_documents)

            if not files:
                return _dump(
                    {
                        "site": site["display_name"],
                        "library": drive["name"],
                        "library_url": drive["web_url"],
                        "documents_found": 0,
                        "note": (
                            "No PDF or Word documents were found in this library. Other "
                            "file types are not readable by this tool."
                        ),
                    }
                )

            documents: list[dict[str, Any]] = []
            total_characters = 0
            for library_file in files:
                entry: dict[str, Any] = library_file.to_dict()
                try:
                    text = await client.read_document(drive["id"], library_file)
                except SharePointError as exc:
                    entry["error"] = str(exc)
                    documents.append(entry)
                    continue

                entry["characters"] = len(text)
                entry["truncated"] = len(text) > characters_per_document
                entry["text"] = (
                    text[:characters_per_document]
                    + "\n[...text truncated by the tool...]"
                    if entry["truncated"]
                    else text
                )
                if not text.strip():
                    entry["note"] = (
                        "No text layer could be extracted - the file is probably a scan "
                        "and would need OCR."
                    )
                total_characters += len(entry["text"])
                documents.append(entry)
    except SharePointError as exc:
        return _dump(
            {
                "error": str(exc),
                "hint": (
                    "Report this to the user and do not invent library contents. "
                    f"Setup: {_SHAREPOINT_SETUP_HINT}"
                ),
            }
        )

    return _dump(
        {
            "site": site["display_name"],
            "library": drive["name"],
            "library_url": drive["web_url"],
            "documents_found": len(files),
            "documents_included": sum(1 for d in documents if "text" in d),
            "characters_returned": total_characters,
            "any_truncated": any(d.get("truncated") for d in documents),
            "documents": documents,
            "note": (
                "These are the raw contents of the user's own SharePoint documents. "
                "Produce ONE combined summary covering all of them, grouped by theme with "
                "the source file named for each point. Distinguish what the documents say "
                "from your own interpretation, and say so if any document was truncated or "
                "could not be read."
            ),
        }
    )


ASX_TOOLS = [
    get_share_research,
    get_share_snapshot,
    get_share_announcements,
    get_share_news,
    get_industry_news,
]
"""The public-data tool surface: price snapshots, announcements and media coverage.

Nothing here needs a credential - every tool reads a public feed - which makes this the
surface that is safe to expose to anyone. The hosted Streamlit deployment attaches only
these tools; see DEPLOY_STREAMLIT_CLOUD.md.
"""

AGENT_TOOLS = [*ASX_TOOLS, read_sharepoint_library]
"""The full tool surface handed to the agent: the ASX tools plus the SharePoint reader.

``read_sharepoint_library`` reads the user's own documents with app-only credentials, so
it stays a separate list rather than being folded into ``ASX_TOOLS``: a deployment that
anyone on the internet can reach has to be able to leave it out.
"""
