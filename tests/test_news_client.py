from asx_sharequery.news_client import build_industry_query


def test_build_industry_query_uses_company_name_and_sector() -> None:
    assert build_industry_query("BHP", "BHP Group Limited", "Materials") == (
        "BHP Group Limited Materials ASX"
    )


def test_build_industry_query_falls_back_to_company_name() -> None:
    assert build_industry_query("CBA", "Commonwealth Bank", None) == (
        "Commonwealth Bank ASX"
    )
