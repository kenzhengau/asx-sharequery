import pytest

from asx_sharequery.price_forecast import ForecastError, build_scenarios


def test_price_scenarios_use_zero_drift_and_horizon_scaled_volatility() -> None:
    prices = [100.0 + index for index in range(40)]

    payload = build_scenarios(10.0, prices, 90)

    assert payload["scenarios_aud"]["base"] == 10.0
    assert payload["scenarios_aud"]["bear"] < 10.0
    assert payload["scenarios_aud"]["bull"] > 10.0
    assert payload["historical_trend_percent"]["20_trading_days"] == round(
        (139.0 / 119.0 - 1) * 100, 2
    )
    assert payload["historical_trend_percent"]["60_trading_days"] is None
    assert "not directional forecasts" in payload["caveat"]


def test_price_scenarios_reject_horizons_outside_supported_range() -> None:
    prices = [100.0 + index for index in range(40)]

    with pytest.raises(ForecastError, match="between 1 and 252"):
        build_scenarios(10.0, prices, 300)