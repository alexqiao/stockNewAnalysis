from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from trade_news_analysis import api
from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.holding_models import HoldingSyncRun
from trade_news_analysis.models import Event, EventSecurityImpact, Security, Watchlist
from trade_news_analysis.risk_models import MarketResearchSnapshot, SecurityRiskProfile
from trade_news_analysis.services.calendar_context import add_calendar_tasks
from trade_news_analysis.services.holdings import read_holdings, save_snapshot
from trade_news_analysis.services.ibkr import BrokerPosition, BrokerSnapshot
from trade_news_analysis.services.market_research import get_market_research
from trade_news_analysis.services.research_data import upsert_calendar
from trade_news_analysis.services.research_inputs import load_research_inputs
from trade_news_analysis.services.research_workflow import read_action_plan, sync_action_tasks
from trade_news_analysis.services.risk import build_risk_plan, get_risk_inputs
from trade_news_analysis.workflow_models import ActionTask

from . import test_decisions_api as api_fixtures
from .test_market_research import NOW
from .test_research_workflow import event_plan
from .test_risk import configure_risk

decisions_client = api_fixtures.decisions_client


@contextmanager
def select_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def collect(
        _connection: Connection, _cursor: Any, statement: str,
        _parameters: Any, _context: Any, _many: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", collect)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", collect)


def test_watchlist_uses_fixed_query_groups_for_one_twenty_and_one_hundred_securities(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        engine = session.get_bind()
        assert isinstance(engine, Engine)
        run = HoldingSyncRun(account_key="batch-account", status="complete")
        session.add(run)
        session.flush()
        save_snapshot(session, run, BrokerSnapshot(
            account_key=run.account_key, account_label="Batch account", captured_at=now,
            currency="USD", net_liquidation=100000, cash_balance=6000, settled_cash=5000,
            available_funds=4000, positions=[BrokerPosition(
                con_id=101, symbol="AAPL", security_type="STK", exchange="NASDAQ", currency="USD",
                quantity=10, average_cost=90, market_price=100, market_value=1000,
                unrealized_pnl=100,
            )],
        ))
        session.execute(delete(Watchlist))
        securities = [Security(
            market="US", exchange="NASDAQ", symbol=f"BATCH{i:03}", name=f"Batch {i}",
        ) for i in range(100)]
        session.add_all(securities)
        session.flush()
        ids = [security.id for security in securities]
        for identifier in ids:
            evidence = Event(
                event_key=f"batch-{identifier}", title="公司公告订单", status="complete",
                demand_status="observed", occurred_at=now,
            )
            session.add(evidence)
            session.flush()
            session.add_all([
                EventSecurityImpact(
                    security_id=identifier, event_id=evidence.id, is_current=True,
                    status="complete", impacts={"5": {"direction": "bullish"}},
                ),
                SecurityRiskProfile(security_id=identifier),
                MarketResearchSnapshot(
                    security_id=identifier, as_of=now, status="blocked",
                    payload={"status": "blocked", "quote": {}, "horizons": {}, "blockers": []},
                ),
            ])
            # More than 500 tasks at 100 securities also exercises history eager loading.
            for horizon in (1, 5, 20):
                for kind in ("valuation", "event"):
                    session.add(ActionTask(
                        task_key=f"batch-{identifier}-{horizon}-{kind}",
                        security_id=identifier, horizon=horizon, kind=kind,
                        review_due_at=now + timedelta(days=30),
                    ))
        session.commit()

    counts = []
    for size in (1, 20, 100):
        with session_factory() as session:
            session.execute(delete(Watchlist))
            session.add_all(Watchlist(security_id=identifier, position=index)
                            for index, identifier in enumerate(ids[:size]))
            session.commit()
        with select_statements(engine) as statements:
            response = decisions_client.get("/api/v1/watchlist")
        assert response.status_code == 200
        assert len(response.json()) == size
        assert all(set(item["risk_plans"]) == {"1", "5", "20"} for item in response.json())
        assert sum("FROM market_research_snapshots" in sql for sql in statements) == 1
        assert sum("FROM holding_snapshots" in sql for sql in statements) == 1
        counts.append(len(statements))
    assert len(set(counts)) == 1, counts
    assert counts[0] <= 25, counts


def test_management_page_does_not_load_research_context(
    decisions_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("管理页面不需要生成行动和风险判断")

    monkeypatch.setattr(api, "build_research_context", unexpected)
    response = decisions_client.get("/watchlist")
    assert response.status_code == 200
    assert "AAPL" in response.text
    assert "待填写估值假设" in response.text


def test_preloaded_inputs_match_individual_readers_across_horizons(
    session: Session, settings: Settings,
) -> None:
    identifier = configure_risk(session, settings)
    security = session.get(Security, identifier)
    assert security is not None
    evidence, plan = event_plan(session)
    for owner in (None, identifier):
        upsert_calendar(
            session, security_id=owner, event_key=f"batch-calendar-{owner}",
            title="Upcoming release", event_type="earnings" if owner else "macro",
            scheduled_date=(NOW + timedelta(days=2)).date(), scheduled_at=None,
            timezone="America/New_York", status="scheduled", source="fixture",
            source_url="https://example.test/calendar", now=NOW - timedelta(days=1),
        )
    for horizon in (1, 5, 20):
        sync_action_tasks(session, identifier, horizon, plan, NOW)
    impacts = session.scalars(select(EventSecurityImpact).where(
        EventSecurityImpact.event_id == evidence.id,
    )).all()
    inputs = load_research_inputs(session, [security], impacts, read_holdings(session, NOW), NOW)
    assert inputs.market[identifier] == get_market_research(session, identifier, NOW)
    risks = get_risk_inputs(session, identifier, NOW, preloaded=inputs.risk)
    assert risks == get_risk_inputs(session, identifier, NOW)
    for horizon in (1, 5, 20):
        assert build_risk_plan(
            session, identifier, horizon=horizon, now=NOW,
            market_data=inputs.market[identifier], risk_inputs=risks,
        ) == build_risk_plan(session, identifier, horizon=horizon, now=NOW)
        assert read_action_plan(
            session, identifier, horizon, plan, NOW, preloaded=inputs.actions,
        ) == read_action_plan(session, identifier, horizon, plan, NOW)
        calendar_plan = {**plan, "horizon": horizon}
        enriched = add_calendar_tasks(
            session, identifier, security.market, calendar_plan, NOW,
            records=inputs.calendars[identifier],
        )
        assert enriched == add_calendar_tasks(
            session, identifier, security.market, calendar_plan, NOW,
        )
        if horizon > 1:
            assert sum(task.get("source_kind") == "research_calendar"
                       for task in enriched["tasks"]) == 2


def test_all_horizon_judgments_match_selected_horizon_requests(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = configure_risk(session, settings)
    security = session.get(Security, identifier)
    assert security is not None

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> FixedTime:
            return cls.fromtimestamp(NOW.timestamp(), tz)

    monkeypatch.setattr(api, "datetime", FixedTime)
    results = [api.build_research_context(
        session, settings, [security], {identifier: "flat"}, horizon,
    )[identifier] for horizon in (1, 5, 20)]
    assert all(row["judgments"] == results[0]["judgments"] for row in results)
    assert all(row["risk_plans"] == results[0]["risk_plans"] for row in results)
