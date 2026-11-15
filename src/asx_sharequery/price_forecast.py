"""Historical-volatility scenarios for ASX share prices.

These are statistical scenarios, not directional predictions or investment advice.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .asx_client import AsxError, normalize_symbol

TRADING_DAYS_PER_YEAR = 252
MAX_HORIZON_DAYS = 252
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}.AX"
_HEADERS = {"User-Agent": "asx-sharequery/0.1 (personal research script)"}
_PEER_GROUPS = (
    ("IXR", "LYC", "ARU", "HAS"),
    ("CBA", "ANZ", "NAB", "WBC", "BOQ"),
    ("BHP", "RIO", "FMG", "S32"),
    ("PLS", "IGO", "LTR", "MIN", "CXO", "GL1"),
    ("NEM", "NST", "EVN", "GOR", "PRU"),
)


class ForecastError(RuntimeError):
    """Raised when historical prices are unavailable or unusable."""


def suggest_peer_symbols(symbol: str) -> list[str]:
    """Return curated ASX comparison tickers for supported peer groups."""
    try:
        code = normalize_symbol(symbol)
    except AsxError:
        return []

    for group in _PEER_GROUPS:
        if code in group:
            return [peer for peer in group if peer != code]
    return []


def build_scenarios(
    current_price: float, adjusted_closes: list[float], horizon_days: int
) -> dict[str, Any]:
    """Build zero-drift, one-standard-deviation scenarios from daily log returns."""
    if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
        raise ForecastError(f"horizon_days must be between 1 and {MAX_HORIZON_DAYS}")
    if current_price <= 0 or not math.isfinite(current_price):
        raise ForecastError("current price is missing or invalid")

    valid_closes = [price for price in adjusted_closes if price > 0 and math.isfinite(price)]
    returns = [math.log(later / earlier) for earlier, later in zip(valid_closes, valid_closes[1:])]
    if len(returns) < 30:
        raise ForecastError("at least 30 daily returns are needed to estimate volatility")

    daily_volatility = statistics.stdev(returns)
    horizon_volatility = daily_volatility * math.sqrt(horizon_days)
    trend_windows = {
        "20_trading_days": 20,
        "60_trading_days": 60,
        "120_trading_days": 120,
    }
    trends = {
        label: (
            round((valid_closes[-1] / valid_closes[-(days + 1)] - 1) * 100, 2)
            if len(valid_closes) > days
            else None
        )
        for label, days in trend_windows.items()
    }
    return {
        "horizon_trading_days": horizon_days,
        "current_price_aud": round(current_price, 4),
        "historical_daily_volatility_percent": round(daily_volatility * 100, 2),
        "historical_trend_percent": trends,
        "scenarios_aud": {
            "bear": round(current_price * math.exp(-horizon_volatility), 4),
            "base": round(current_price, 4),
            "bull": round(current_price * math.exp(horizon_volatility), 4),
        },
        "method": (
            "The base holds the current price constant. Bear and bull are one historical "
            "standard deviation below and above it, using daily log-return volatility "
            "scaled by the square root of the requested trading days."
        ),
        "caveat": (
            "These are volatility scenarios, not directional forecasts, price targets, "
            "or probabilities. They assume zero expected return and stable historical "
            "volatility; actual prices can fall outside this range."
        ),
    }


async def get_price_scenarios(
    symbol: str, horizon_days: int = 90, *, timeout: float = 20.0
) -> dict[str, Any]:
    """Fetch one year of daily prices and calculate historical-volatility scenarios."""
    try:
        code = normalize_symbol(symbol)
    except AsxError as exc:
        raise ForecastError(str(exc)) from exc
    if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
        raise ForecastError(f"horizon_days must be between 1 and {MAX_HORIZON_DAYS}")

    result = await _fetch_chart_result(code, "1y", timeout)

    try:
        quote_data = result["indicators"]["quote"][0]
        adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
        closes = [float(value) for value in adjusted if value is not None]
        market_price = result["meta"].get("regularMarketPrice")
        current_price = float(
            market_price if market_price is not None else quote_data["close"][-1]
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ForecastError("the price-history provider returned incomplete data") from exc

    scenarios = build_scenarios(current_price, closes, horizon_days)
    return {
        "symbol": f"{code}.AX",
        "source": "Yahoo Finance chart data",
        "history_period": "1 year of daily adjusted closes",
        "source_url": f"https://finance.yahoo.com/quote/{code}.AX/history/",
        **scenarios,
    }


async def _fetch_chart_result(code: str, period: str, timeout: float) -> dict[str, Any]:
    """Fetch one Yahoo chart response and return its first result object."""
    url = _CHART_URL.format(quote(code))
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout) as client:
            response = await client.get(url, params={"range": period, "interval": "1d"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ForecastError(
            f"could not retrieve historical prices ({exc.__class__.__name__})"
        ) from exc

    try:
        chart = response.json()["chart"]
        if chart.get("error"):
            raise ForecastError("the price-history provider returned an error")
        result = chart["result"][0]
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ForecastError("the price-history provider returned incomplete data") from exc


async def get_price_history(
    symbol: str, period: str = "6mo", *, timeout: float = 20.0
) -> dict[str, Any]:
    """Fetch daily closing prices for charting an ASX share."""
    allowed_periods = {"1mo", "3mo", "6mo", "1y"}
    if period not in allowed_periods:
        raise ForecastError("period must be one of 1mo, 3mo, 6mo or 1y")
    try:
        code = normalize_symbol(symbol)
    except AsxError as exc:
        raise ForecastError(str(exc)) from exc

    result = await _fetch_chart_result(code, period, timeout)
    try:
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        points = [
            {
                "date": datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat(),
                "price": float(price),
            }
            for timestamp, price in zip(timestamps, closes)
            if price is not None and float(price) > 0
        ]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise ForecastError("the price-history provider returned incomplete data") from exc
    if len(points) < 2:
        raise ForecastError(f"not enough price history was available for {code}")

    return {
        "symbol": f"{code}.AX",
        "source": "Yahoo Finance chart data",
        "history_period": period,
        "source_url": f"https://finance.yahoo.com/quote/{code}.AX/history/",
        "points": points,
    }