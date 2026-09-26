"""Live smoke test for the ASX data layer. Does not need a model API token.

Usage:
    uv run python scripts/smoke_test.py [SYMBOL]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys

import httpx

from asx_sharequery.asx_client import (
    AsxClient,
    UnknownSymbolError,
    company_page_url,
    normalize_symbol,
)
from asx_sharequery.news_client import get_share_news
from asx_sharequery.tools import AGENT_TOOLS

SYMBOL = (sys.argv[1] if len(sys.argv) > 1 else "BHP").upper()


def check_normalization() -> None:
    print("== symbol normalisation ==")
    for raw in [" bhp ", "bhp.ax", "CBA", "a2m"]:
        print(f"   {raw!r:10} -> {normalize_symbol(raw)!r}")
    for bad in ["", "TOOLONGCODE", "BH P", "!!"]:
        try:
            normalize_symbol(bad)
            print(f"   {bad!r:14} -> NO ERROR (unexpected!)")
        except UnknownSymbolError:
            print(f"   {bad!r:14} -> correctly rejected")


async def check_client(symbol: str) -> list[str]:
    print(f"\n== AsxClient ({symbol}) ==")
    async with AsxClient() as client:
        snapshot = await client.get_snapshot(symbol.lower() + ".ax")
        print("   snapshot:", json.dumps(snapshot.to_dict())[:220], "...")

        announcements = await client.get_announcements(symbol)
        print(f"   announcements: {len(announcements)}")
        for item in announcements:
            published = item.published.date() if item.published else "?"
            flag = "PRICE-SENSITIVE" if item.price_sensitive else "routine"
            print(f"     - {published} [{item.announcement_type}] {item.headline} ({flag})")

    for bogus in ("ZZZZ", "QQQQQQ"):
        try:
            async with AsxClient() as client:
                await client.get_snapshot(bogus)
            print(f"   bogus {bogus}: NO ERROR (unexpected!)")
        except UnknownSymbolError as exc:
            print(f"   bogus {bogus}: {type(exc).__name__} -> {exc}")

    return [company_page_url(symbol)]


async def check_tools(symbol: str) -> None:
    print(f"\n== tools ({symbol}) ==")
    for tool in AGENT_TOOLS:
        func = getattr(tool, "func", None)
        print(f"   {tool.name}: func={'yes' if func else 'NO'} async={inspect.iscoroutinefunction(func)}")

    by_name = {tool.name: tool for tool in AGENT_TOOLS}
    for name in ("get_share_research", "get_share_snapshot", "get_share_announcements"):
        raw = await by_name[name].func(symbol)
        if not isinstance(raw, str):
            raise AssertionError(f"{name} returned {type(raw)}, expected str")
        payload = json.loads(raw)  # a real failure would raise here
        print(f"   {name}: {len(raw):>6} chars of valid JSON")

    bad = json.loads(await by_name["get_share_research"].func("ZZZZ"))
    print(f"   get_share_research('ZZZZ') -> clear error returned: {'error' in bad}")

    good = json.loads(await by_name["get_share_research"].func(symbol))
    print(
        "   get_share_research(%s) -> price=%s announcements=%s source_url=%s"
        % (
            symbol,
            good["price"]["name"],
            good["announcements"]["count"],
            good["source_url"],
        )
    )


async def check_news(symbol: str) -> None:
    print(f"\n== media headlines ({symbol}) ==")
    items = await get_share_news(symbol)
    print(f"   {len(items)} relevant headlines")
    for item in items[:5]:
        date = item.published.date() if item.published else "?"
        print(f"     - {date} [{item.source or '?'}] {item.headline[:66]}")

    by_name = {tool.name: tool for tool in AGENT_TOOLS}
    payload = json.loads(await by_name["get_share_news"].func(symbol))
    print(f"   tool query: {payload['query']!r} -> {payload['count']} items")

    # A junk code must return an empty list rather than unrelated news.
    junk = await get_share_news("ZZZZ", None)
    print(f"   unrelated code ZZZZ -> {len(junk)} items (expected 0)")


async def check_links(urls: list[str]) -> None:
    print("\n== ASX company page link ==")
    if not urls:
        print("   none to check")
        return
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        for url in urls:
            try:
                response = await client.head(url)
                print(f"   [{response.status_code}] {url}")
            except httpx.HTTPError as exc:
                print(f"   [error] {url} -> {exc}")


async def main() -> int:
    check_normalization()
    urls = await check_client(SYMBOL)
    await check_tools(SYMBOL)
    await check_news(SYMBOL)
    await check_links(urls)
    print("\nAll ASX data-layer checks completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
