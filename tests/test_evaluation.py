from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Article, ArticleSymbol, ImpactAnalysis, Outcome
from trade_news_analysis.services.evaluation import OutcomeEvaluator, first_tradable_date


def frame(open_start: float, close_step: float) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-05", periods=30)
    opens = [open_start + i for i in range(30)]
    closes = [value + close_step for value in opens]
    return pd.DataFrame({"Open": opens, "Close": closes}, index=index)


def add_complete_analysis(session: Session, created_at: datetime) -> ImpactAnalysis:
    article = Article(
        fingerprint="e" * 64,
        canonical_url="https://example.com/e",
        source="Wire",
        title="Apple demand rises",
        summary="Paid users increased.",
        story_cluster_id="cluster-e",
    )
    relation = ArticleSymbol(article=article, symbol="AAPL", in_title=True)
    analysis = ImpactAnalysis(
        article_symbol=relation,
        status="complete",
        model="test",
        created_at=created_at,
        impacts={
            "1": {"direction": "bullish", "confidence": 0.8, "reason": "x"},
            "5": {"direction": "bullish", "confidence": 0.8, "reason": "x"},
            "20": {"direction": "bullish", "confidence": 0.8, "reason": "x"},
        },
    )
    session.add(analysis)
    session.commit()
    return analysis


def test_first_tradable_date_avoids_after_hours_lookahead() -> None:
    dates = [stamp.date() for stamp in pd.bdate_range("2026-01-05", periods=4)]
    before_open = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)  # 08:00 New York
    after_open = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)  # 11:00 New York
    assert first_tradable_date(before_open, dates) == dates[0]
    assert first_tradable_date(after_open, dates) == dates[1]


def test_evaluator_creates_idempotent_horizons(session: Session) -> None:
    add_complete_analysis(session, datetime(2026, 1, 4, 12, tzinfo=UTC))

    def loader(symbol: str) -> pd.DataFrame:
        return frame(100, 3) if symbol == "AAPL" else frame(100, 0)

    evaluator = OutcomeEvaluator(loader)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert evaluator.evaluate(session, now=now) == 3
    assert evaluator.evaluate(session, now=now) == 0
    assert session.scalar(select(func.count()).select_from(Outcome)) == 3
    one_day = session.scalar(select(Outcome).where(Outcome.horizon == 1))
    assert one_day is not None
    assert one_day.predicted_direction == "bullish"
    assert one_day.actual_direction == "bullish"
    assert one_day.correct is True


def test_evaluator_does_not_write_without_benchmark(session: Session) -> None:
    add_complete_analysis(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(lambda _symbol: pd.DataFrame())
    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0
    assert session.scalar(select(func.count()).select_from(Outcome)) == 0
