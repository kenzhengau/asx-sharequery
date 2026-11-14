from asx_sharequery.news_client import NewsItem, build_industry_query
from asx_sharequery.tools import _share_research_news_payloads


def test_build_industry_query_uses_company_name_and_sector() -> None:
    assert build_industry_query("BHP", "BHP Group Limited", "Materials") == (
        "BHP Group Limited Materials ASX"
    )


def test_build_industry_query_falls_back_to_company_name() -> None:
    assert build_industry_query("CBA", "Commonwealth Bank", None) == (
        "Commonwealth Bank ASX"
    )


def test_share_research_news_payloads_include_company_and_industry_context() -> None:
    company_items = [
        NewsItem(
            headline="IXR eyes rare earth supply chain",
            source="The Australian",
            published=None,
            url=None,
        )
    ]
    industry_items = [
        NewsItem(
            headline="Rare earth prices rise on supply concerns",
            source="Bloomberg",
            published=None,
            url=None,
        )
    ]

    payload = _share_research_news_payloads(
        "IXR",
        "Ionic Rare Earths Limited",
        "Materials",
        company_items,
        industry_items,
    )

    assert payload["media_coverage"]["query"] == "Ionic Rare Earths Limited ASX"
    assert payload["industry_context"]["query"] == "Ionic Rare Earths Limited Materials ASX"
    assert payload["media_coverage"]["count"] == 1
    assert payload["industry_context"]["count"] == 1
