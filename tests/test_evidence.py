from __future__ import annotations

from trade_news_analysis.models import Article
from trade_news_analysis.services.evidence import assess_articles, source_identity


def article(
    source: str,
    *,
    role: str = "reporting",
    kind: str = "news",
    eligible: bool = True,
) -> Article:
    return Article(
        fingerprint=(source + role).encode().hex().ljust(64, "0")[:64],
        canonical_url="https://example.com",
        source=source,
        title="Evidence",
        story_cluster_id="cluster",
        evidence_role=role,
        content_kind=kind,
        analysis_eligible=eligible,
    )


def test_evidence_ladder_prefers_primary_sources() -> None:
    strong = assess_articles([article("SEC EDGAR")])
    medium = assess_articles([article("Reuters")])
    weak = assess_articles(
        [article("X:@analyst", role="social_lead", kind="social_post")]
    )

    assert (strong.grade, strong.score) == ("strong", 4.5)
    assert (medium.grade, medium.score) == ("medium", 2.5)
    assert (weak.grade, weak.score) == ("weak", 1.0)


def test_evidence_sources_are_deduplicated_and_ineligible_rows_are_ignored() -> None:
    yahoo = article("Yahoo Finance")
    duplicate = article("Yahoo Finance Video")
    ignored = article("SEC EDGAR", eligible=False)

    result = assess_articles([yahoo, duplicate, ignored])

    assert source_identity(yahoo) == source_identity(duplicate)
    assert result.independent_sources == 0
    assert result.report_sources == 1
    assert result.unknown_original_sources == 1
    assert result.score == 1.0


def test_reports_of_the_same_original_do_not_raise_independent_evidence_score() -> None:
    reports = [article("Reuters"), article("Bloomberg"), article("Financial Times")]
    for report in reports:
        report.original_source_url = "https://www.sec.gov/Archives/filing.htm"
    result = assess_articles(reports)
    assert result.report_sources == 3
    assert result.original_sources == result.independent_sources == 1
    assert result.unknown_original_sources == 0
    assert result.score == 2.5
    reports[-1].original_source_url = "https://www.sec.gov/Archives/other-filing.htm"
    independent = assess_articles(reports)
    assert independent.original_sources == 2
    assert independent.score > result.score


def test_unknown_attribution_is_visible_without_invented_independence() -> None:
    result = assess_articles([article("Reuters"), article("Bloomberg")])
    assert result.report_sources == result.unknown_original_sources == 2
    assert result.original_sources == 0
    assert result.score == 2.5


def test_unknown_reports_do_not_increase_primary_evidence_score() -> None:
    primary = article("SEC EDGAR")
    result = assess_articles([primary, article("Reuters"), article("Bloomberg")])
    assert result.score == 4.5
    assert result.original_sources == 1
    assert result.report_count == 3
    assert result.unknown_original_sources == 2
