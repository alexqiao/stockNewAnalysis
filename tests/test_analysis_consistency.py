from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.models import Event, EventSecurityImpact, Security, Theme
from trade_news_analysis.schemas import CandidateCompany
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.scoring import rebuild_signal_snapshots

from .test_analysis import VALID_EVENT_PAYLOAD, VALID_IMPACT_PAYLOAD, add_pending_event


def payload(symbols: list[str], theme: str = "企业软件") -> dict[str, Any]:
    return {
        **VALID_EVENT_PAYLOAD,
        "themes": [theme],
        "candidates": [
            {
                "name": {"AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation"}.get(symbol, symbol),
                "symbol": symbol,
                "market": "US",
                "supply_chain_role": "直接提供付费服务",
                "chain_level": 1,
                "themes": [theme],
            }
            for symbol in symbols
        ],
    }


@pytest.mark.parametrize("remaining", [["MSFT"], [], ["UNVERIFIED"]])
def test_reanalysis_replaces_candidates_themes_and_signal_in_same_session(
    session: Session, settings: Settings, remaining: list[str],
) -> None:
    event = add_pending_event(session)
    current_payload = payload(["AAPL", "MSFT"])

    def complete(_system: str, prompt: str) -> str:
        return json.dumps(current_payload if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD)

    analyzer = EventAnalyzer(settings, completion=complete)
    analyzer.analyze_event(session, event)
    apple = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert apple is not None
    # Populate relationships to ensure a same-session rebuild observes replacements.
    assert len(apple.impacts) == 1
    assert [link.theme.name for link in event.theme_links] == ["企业软件"]
    before = rebuild_signal_snapshots(session)
    assert any(item.security_id == apple.id and item.direction == "bullish" for item in before)
    current_payload = payload(remaining, "数据中心GPU")
    analyzer.analyze_event(session, event)
    current = session.scalars(select(EventSecurityImpact).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    )).all()
    assert {item.security.symbol for item in current} == set(remaining) - {"UNVERIFIED"}
    assert [link.theme.name for link in event.theme_links] == ["数据中心GPU"]
    assert len(apple.impacts) == 1  # The historical row remains, but no longer contributes.
    assert apple.impacts[0].is_current is False
    after = rebuild_signal_snapshots(session)
    apple_signals = [item for item in after if item.security_id == apple.id]
    assert len(apple_signals) == 3
    assert all(
        item.direction == "neutral" and not item.evidence_event_ids for item in apple_signals
    )


def test_share_class_must_not_fall_back_to_other_ticker_or_shared_name(session: Session) -> None:
    session.add(Security(
        market="US", exchange="NYSE", symbol="BRK.A", name="Berkshire Hathaway",
        aliases=["Berkshire"],
    ))
    session.flush()
    candidate = CandidateCompany(
        name="Berkshire Hathaway", symbol="BRK.B", market="US",
        supply_chain_role="投资持股", chain_level=1, themes=["保险"],
    )
    assert EventAnalyzer._candidate_security(session, candidate) is None
    candidate.symbol = None
    assert EventAnalyzer._candidate_security(session, candidate) is not None
    candidate.symbol = "BRK.A.US"
    assert EventAnalyzer._candidate_security(session, candidate) is not None
    candidate.symbol = "BRK-A"
    assert EventAnalyzer._candidate_security(session, candidate) is not None
    candidate.symbol = "BRK-B"
    assert EventAnalyzer._candidate_security(session, candidate) is None
    session.add(Security(
        market="US", exchange="NYSE", symbol="BRK.B", name="Berkshire Hathaway",
    ))
    session.flush()
    matched = EventAnalyzer._candidate_security(session, candidate)
    assert matched is not None and matched.symbol == "BRK.B"


def test_only_transient_failed_security_is_retried_with_bounded_backoff(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    calls: list[str] = []
    clock = datetime(2026, 9, 29, 2, tzinfo=UTC)

    def complete(_system: str, prompt: str) -> str:
        if "canonical_title" in prompt:
            calls.append("discovery")
            return json.dumps(payload(["AAPL", "MSFT"]))
        if "证券：Apple" in prompt:
            calls.append("AAPL")
            return json.dumps(VALID_IMPACT_PAYLOAD)
        calls.append("MSFT")
        raise TimeoutError("provider temporarily unavailable")

    analyzer = EventAnalyzer(settings, completion=complete)
    with patch("trade_news_analysis.services.analysis.utc_now", return_value=clock):
        analyzer.analyze_event(session, event)
        assert event.status == "partial"
        assert event.analysis_attempts == 1
        assert event.analysis_next_retry_at == clock + timedelta(minutes=1)
        assert analyzer.analyze_pending(session) == 0
    success_id = session.scalar(select(EventSecurityImpact.id).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.status == "complete",
    ))
    clock += timedelta(minutes=1)
    with patch("trade_news_analysis.services.analysis.utc_now", return_value=clock):
        assert analyzer.analyze_pending(session) == 1
        assert event.analysis_next_retry_at == clock + timedelta(minutes=5)
    clock += timedelta(minutes=5)
    with patch("trade_news_analysis.services.analysis.utc_now", return_value=clock):
        assert analyzer.analyze_pending(session) == 1
        assert event.analysis_attempts == 3
        assert event.analysis_next_retry_at is None
        assert analyzer.analyze_pending(session) == 0
    assert calls == ["discovery", "AAPL", "MSFT", "MSFT", "MSFT"]
    assert session.scalar(select(EventSecurityImpact.id).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.status == "complete",
        EventSecurityImpact.is_current.is_(True),
    )) == success_id


@pytest.mark.parametrize("transient", [False, True])
def test_failed_reanalysis_never_reuses_old_success_and_recovery_can_complete(
    session: Session, settings: Settings, transient: bool,
) -> None:
    event = add_pending_event(session)
    fail = False
    clock = datetime(2026, 9, 29, 2, tzinfo=UTC)

    def complete(_system: str, prompt: str) -> str:
        if "canonical_title" in prompt:
            return json.dumps(payload(["AAPL"]))
        if fail:
            raise TimeoutError("slow source") if transient else ValueError("invalid response")
        return json.dumps(VALID_IMPACT_PAYLOAD)

    analyzer = EventAnalyzer(settings, completion=complete)
    analyzer.analyze_event(session, event)
    fail = True
    with patch("trade_news_analysis.services.analysis.utc_now", return_value=clock):
        analyzer.analyze_event(session, event)
    assert event.status == "error"
    current = session.scalar(select(EventSecurityImpact).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    ))
    assert current is not None and current.status == "error"
    assert current.retryable is transient
    fail = False
    with patch(
        "trade_news_analysis.services.analysis.utc_now", return_value=clock + timedelta(days=1),
    ):
        assert analyzer.analyze_pending(session) == int(transient)
    assert event.status == ("complete" if transient else "error")
    assert event.analysis_next_retry_at is None


def test_discovery_timeout_retries_discovery_and_manual_analysis_resets_budget(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    fail = True
    calls = 0
    clock = datetime(2026, 9, 29, 2, tzinfo=UTC)

    def complete(_system: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        if fail:
            raise TimeoutError("temporary")
        return json.dumps(payload([]))

    analyzer = EventAnalyzer(settings, completion=complete)
    with patch("trade_news_analysis.services.analysis.utc_now", return_value=clock):
        analyzer.analyze_event(session, event)
    assert event.status == "error" and event.analysis_stage == "discovery"
    fail = False
    with patch(
        "trade_news_analysis.services.analysis.utc_now", return_value=clock + timedelta(minutes=1),
    ):
        assert analyzer.analyze_pending(session) == 1
    assert event.status == "complete" and calls == 2
    event.analysis_attempts = 3
    analyzer.analyze_event(session, event)
    assert event.analysis_attempts == 1 and event.status == "complete"


def test_unconfigured_reanalysis_cannot_keep_previous_current_success(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=lambda _system, prompt: json.dumps(
        payload(["AAPL"]) if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    )).analyze_event(session, event)
    EventAnalyzer(settings).analyze_event(session, event)
    assert event.status == "unavailable"
    assert not any(impact.is_current for impact in event.impacts)


@pytest.mark.parametrize("fail_discovery", [False, True])
def test_discovery_does_not_hold_write_lock_and_failure_still_withdraws_old_success(
    session: Session, settings: Settings, fail_discovery: bool,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=lambda _system, prompt: json.dumps(
        payload(["AAPL"]) if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    )).analyze_event(session, event)
    previous = session.scalar(select(EventSecurityImpact).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    ))
    assert previous is not None
    saved: list[str] = []
    session.expire(event, ["article_links"])

    def complete(_system: str, prompt: str) -> str:
        if "canonical_title" not in prompt:
            return json.dumps(VALID_IMPACT_PAYLOAD)
        with Session(session.get_bind()) as other:
            other.execute(text("PRAGMA busy_timeout=50"))
            other.execute(text("UPDATE securities SET industry='review' WHERE symbol='MSFT'"))
            other.commit()
        saved.append("updated")
        if fail_discovery:
            raise TimeoutError("temporary discovery failure")
        return json.dumps(payload(["AAPL"]))

    EventAnalyzer(settings, completion=complete).analyze_event(session, event)
    assert saved == ["updated"]
    assert previous.is_current is False
    assert event.status == ("error" if fail_discovery else "complete")
    if fail_discovery:
        assert not any(impact.is_current for impact in event.impacts)
        assert event.analysis_next_retry_at is not None


@pytest.mark.parametrize("second_result", ["success", "timeout", "repair", "invalid_repair"])
def test_all_security_completions_allow_concurrent_writes_and_replace_results_atomically(
    session: Session, settings: Settings, second_result: str,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=lambda _system, prompt: json.dumps(
        payload(["AAPL", "MSFT"]) if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    )).analyze_event(session, event)
    original_title = event.title
    previous_ids = set(session.scalars(select(EventSecurityImpact.id).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    )))
    assert len(previous_ids) == 2
    revised = payload(["AAPL", "MSFT"])
    revised["canonical_title"] = "更新后的企业需求事件"
    revised["themes"] = ["企业软件", "数据中心GPU"]
    revised["candidates"][1]["themes"] = ["数据中心GPU"]
    writes: list[str] = []
    session.expire_all()

    def complete(_system: str, prompt: str) -> str:
        with Session(session.get_bind()) as other:
            other.execute(text("PRAGMA busy_timeout=50"))
            assert other.scalar(select(Event.title).where(Event.id == event.id)) == original_title
            assert set(other.scalars(select(EventSecurityImpact.id).where(
                EventSecurityImpact.event_id == event.id,
                EventSecurityImpact.is_current.is_(True),
            ))) == previous_ids
            assert other.scalar(select(func.count()).select_from(EventSecurityImpact)) == 2
            assert other.scalar(select(Theme.id).where(Theme.name == "数据中心GPU")) is None
            other.execute(text("UPDATE securities SET industry=:value WHERE symbol='MSFT'"), {
                "value": f"concurrent-{len(writes)}",
            })
            other.commit()
        writes.append(prompt)
        if "canonical_title" in prompt:
            return json.dumps(revised)
        if prompt.startswith("以下输出未通过结构校验"):
            return "still-invalid" if second_result == "invalid_repair" else json.dumps(
                VALID_IMPACT_PAYLOAD
            )
        if "证券：Microsoft" in prompt:
            if second_result == "timeout":
                raise TimeoutError("temporary impact failure")
            if second_result in {"repair", "invalid_repair"}:
                return "not-json"
        return json.dumps(VALID_IMPACT_PAYLOAD)

    EventAnalyzer(settings, completion=complete).analyze_event(session, event)

    assert len(writes) == (4 if second_result in {"repair", "invalid_repair"} else 3)
    failed = second_result in {"timeout", "invalid_repair"}
    assert event.status == ("partial" if failed else "complete")
    assert event.title == revised["canonical_title"]
    assert (event.analysis_next_retry_at is not None) is (second_result == "timeout")
    current = list(session.scalars(select(EventSecurityImpact).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    )))
    assert len(current) == 2 and not previous_ids.intersection(row.id for row in current)
    assert not list(session.scalars(select(EventSecurityImpact.id).where(
        EventSecurityImpact.id.in_(previous_ids), EventSecurityImpact.is_current.is_(True),
    )))
    assert {link.theme.name for link in event.theme_links} == {"企业软件", "数据中心GPU"}
    by_symbol = {row.security.symbol: row for row in current}
    assert [link.theme.name for link in by_symbol["AAPL"].theme_links] == ["企业软件"]
    assert [link.theme.name for link in by_symbol["MSFT"].theme_links] == ["数据中心GPU"]
    assert by_symbol["AAPL"].status == "complete"
    assert by_symbol["MSFT"].status == ("error" if failed else "complete")
    assert by_symbol["MSFT"].retryable is (second_result == "timeout")


def test_persistence_failure_rolls_back_the_entire_event_replacement(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    initial = payload(["AAPL", "MSFT"])
    EventAnalyzer(settings, completion=lambda _system, prompt: json.dumps(
        initial if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    )).analyze_event(session, event)
    original_title = event.title
    previous_ids = set(session.scalars(select(EventSecurityImpact.id).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    )))
    microsoft_id = session.scalar(select(Security.id).where(Security.symbol == "MSFT"))
    revised = payload(["AAPL", "MSFT"], "数据中心GPU")
    revised["canonical_title"] = "不能部分提交的更新"
    calls: list[str] = []

    def complete(_system: str, prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(revised if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD)

    def reject_second_impact(
        current: Session, _context: object, _instances: object,
    ) -> None:
        if any(isinstance(item, EventSecurityImpact) and item.security_id == microsoft_id
               for item in current.new):
            raise RuntimeError("synthetic persistence failure")

    sqlalchemy_event.listen(session, "before_flush", reject_second_impact)
    try:
        with pytest.raises(RuntimeError, match="synthetic persistence failure"):
            EventAnalyzer(settings, completion=complete).analyze_event(session, event)
    finally:
        sqlalchemy_event.remove(session, "before_flush", reject_second_impact)
        session.rollback()
    assert len(calls) == 3
    assert event.title == original_title and event.status == "complete"
    assert set(session.scalars(select(EventSecurityImpact.id).where(
        EventSecurityImpact.event_id == event.id, EventSecurityImpact.is_current.is_(True),
    ))) == previous_ids
    assert session.scalar(select(func.count()).select_from(EventSecurityImpact)) == 2
    assert session.scalar(select(Theme.id).where(Theme.name == "数据中心GPU")) is None
    assert [link.theme.name for link in event.theme_links] == ["企业软件"]
