from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Event, Security, SecuritySignalSnapshot
from trade_news_analysis.research_data_models import ResearchSourceState
from trade_news_analysis.risk_models import ActionDecisionSnapshot, SecurityRiskProfile
from trade_news_analysis.workflow_models import ActionTask

from . import test_decisions_api as fixtures

decision_security = fixtures.decision_security
decisions_client = fixtures.decisions_client


def test_capture_tasks_is_persistent_and_reads_do_not_create_records(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    security_id, _ = decision_security
    before = decisions_client.get(f"/api/v1/securities/{security_id}").json()
    assert before["judgment"]["strategy"]["execution"]["ready"] is False
    with session_factory() as session:
        assert session.scalar(select(func.count(ActionTask.id))) == 0
        assert session.scalar(select(func.count(ActionDecisionSnapshot.id))) == 0
    response = decisions_client.post("/api/v1/research/capture")
    assert response.status_code == 200
    after = decisions_client.get(f"/api/v1/securities/{security_id}").json()
    task = after["judgment"]["action"]["plan"]["tasks"][0]
    assert task["id"] is not None
    response = decisions_client.patch(
        f"/api/v1/research/tasks/{task['id']}",
        json={"status": "done", "note": "已打开原文核对", "expected_revision": task["revision"]},
    )
    assert response.status_code == 200
    reloaded = decisions_client.get(f"/api/v1/securities/{security_id}").json()
    done = next(
        item for item in reloaded["judgment"]["action"]["plan"]["tasks"] if item["id"] == task["id"]
    )
    assert done["status"] == "done"
    assert done["note"] == "已打开原文核对"
    assert reloaded["judgment"]["strategy"]["execution"]["ready"] is False
    with session_factory() as session:
        count = session.scalar(select(func.count(ActionDecisionSnapshot.id)))
    decisions_client.post("/api/v1/research/capture")
    with session_factory() as session:
        assert session.scalar(select(func.count(ActionDecisionSnapshot.id))) == count


def test_fact_timing_cannot_be_shifted_forward_by_republication(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    security_id, _ = decision_security
    with session_factory() as session:
        event = session.scalar(select(Event).where(Event.event_key == "decision-api-order"))
        event_id = event.id
    first = datetime.now(UTC) - timedelta(days=120)
    payload = {
        "first_disclosed_at": first.isoformat(),
        "source_url": "https://www.sec.gov/Archives/example.htm",
        "financial_period": "2026Q1",
        "note": "原文首次披露该订单",
    }
    response = decisions_client.put(f"/api/v1/research/events/{event_id}/timing", json=payload)
    assert response.status_code == 200
    with session_factory() as session:
        latest = session.scalar(
            select(SecuritySignalSnapshot)
            .where(
                SecuritySignalSnapshot.security_id == security_id,
                SecuritySignalSnapshot.horizon == 5,
            )
            .order_by(SecuritySignalSnapshot.id.desc())
        )
        assert latest.evidence_event_ids == []
    payload["first_disclosed_at"] = datetime.now(UTC).isoformat()
    response = decisions_client.put(f"/api/v1/research/events/{event_id}/timing", json=payload)
    assert response.status_code == 422


def test_risk_inputs_validate_values_and_remain_unknown_without_prices(
    decisions_client: TestClient,
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
    response = decisions_client.put(
        "/api/v1/research/portfolio",
        json={
            "total_value": 100000,
            "currency": "USD",
            "available_cash": 20000,
        },
    )
    assert response.status_code == 200
    endpoint = f"/api/v1/research/securities/{security_id}/risk"
    assert decisions_client.put(endpoint, json={"max_weight": 5}).status_code == 422
    assert (
        decisions_client.put(endpoint, json={"max_weight": 0.05, "fee_bps": 0}).status_code == 200
    )
    result = decisions_client.get(f"/api/v1/securities/{security_id}").json()
    assert result["risk_plan"]["max_buy_quantity"] is None
    assert result["risk_plan"]["blockers"]


def test_source_coverage_distinguishes_stale_restricted_and_disabled_refresh(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        sid = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        for key, status in [
            ("bls_actual:CES0000000001", "restricted"),
            ("bls_actual:LNS14000000", "restricted"),
            ("finnhub_earnings:1", "available"),
        ]:
            session.add(ResearchSourceState(
                source_key=key, security_id=None, capability="test", status=status,
                last_attempt_at=now - timedelta(days=1),
                last_success_at=now - timedelta(days=1) if status == "available" else None,
                expires_at=now - timedelta(hours=18),
            ))
        session.commit()
    detail = decisions_client.get(f"/api/v1/research/securities/{sid}").json()
    assert detail["refresh_schedule"]["enabled"] is False
    assert {row["status"] for row in detail["data"]["source_health"]} == {"stale", "restricted"}
    page = decisions_client.get(f"/research?security_id={sid}").text
    assert "BLS 非农就业人数" in page and "BLS 失业率" in page
    assert "访问受限 2 项" in page and "待刷新 1 项" in page
    assert "自动刷新未开启" in page
    assert "· stale" not in page


def test_risk_defaults_render_and_partial_edits_preserve_automatic_fields(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    from bs4 import BeautifulSoup

    with session_factory() as session:
        sid = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
    endpoint = f"/api/v1/research/securities/{sid}"
    initial = decisions_client.get(endpoint).json()["risk_inputs"]
    assert initial["security"]["max_weight"] == 0.2
    assert initial["field_sources"]["max_weight"]["kind"] == "default"
    page = BeautifulSoup(decisions_client.get(f"/research?security_id={sid}").text, "html.parser")
    form = page.select_one('[data-risk-defaults="true"]')
    assert form.select_one('[name="max_weight"]')["value"] == "0.2"
    assert form.select_one('[name="fee_bps"]')["value"] == "10"
    assert form.select_one('[name="benchmark_symbol"]')["value"] == "XLK"
    assert "默认" in form.select_one("#risk-note-max_weight").text
    with session_factory() as session:
        assert session.scalar(select(func.count(SecurityRiskProfile.id))) == 0
    updated = decisions_client.put(endpoint + "/risk", json={"max_weight": 0.3, "fee_bps": 0})
    assert updated.status_code == 200
    result = decisions_client.get(endpoint).json()["risk_inputs"]
    assert result["security"]["max_weight"] == 0.3
    assert result["security"]["fee_bps"] == 0
    assert result["field_sources"]["max_weight"]["kind"] == "manual"
    with session_factory() as session:
        profile = session.scalar(select(SecurityRiskProfile))
        assert profile.risk_budget_pct is None
        assert profile.stop_price is None
        assert profile.benchmark_symbol is None
    cleared = decisions_client.put(endpoint + "/risk", json={"max_weight": None}).json()
    assert cleared["security"]["max_weight"] == 0.2
    assert cleared["field_sources"]["max_weight"]["kind"] == "default"


def test_workbench_manual_disclosure_calendar_and_actual_input(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
) -> None:
    security_id, _ = decision_security
    base = f"/api/v1/research/securities/{security_id}"
    response = decisions_client.post(
        base + "/disclosures",
        json={
            "title": "官方财报",
            "source_url": "https://www.sec.gov/Archives/example.htm",
            "excerpt": None,
        },
    )
    assert response.status_code == 200
    assert response.json()["content_status"] == "link_only"
    future = datetime.now(UTC) + timedelta(days=5)
    response = decisions_client.post(
        base + "/calendar",
        json={
            "event_key": "2026Q3-earnings",
            "title": "季度财报",
            "event_type": "earnings",
            "scheduled_date": future.date().isoformat(),
            "scheduled_at": future.isoformat(),
            "timezone": "UTC",
            "status": "estimated",
            "source_url": "https://www.sec.gov/Archives/example.htm",
        },
    )
    assert response.status_code == 200
    event_key = response.json()["event_key"]
    response = decisions_client.post(
        base + "/actuals",
        json={
            "event_key": event_key,
            "metric": "eps",
            "value": 0,
            "unit": "USD/share",
            "financial_period": "2026Q3",
            "source_url": "https://www.sec.gov/Archives/example.htm",
            "published_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.json()["details"]["epsActual"] == 0
    response = decisions_client.post(
        base + "/actuals",
        json={
            "event_key": event_key,
            "metric": "revenue",
            "value": 500,
            "unit": "USD",
            "financial_period": "2026Q4",
            "source_url": "https://www.sec.gov/Archives/example.htm",
            "published_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 422
    latest = decisions_client.get(base).json()["data"]["calendar"][0]
    assert latest["details"]["financial_period"] == "2026Q3"
    assert "revenueActual" not in latest["details"]
    eps_published = latest["details"]["eps_published_at"]
    revenue_published = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    response = decisions_client.post(
        base + "/actuals",
        json={
            "event_key": event_key, "metric": "revenue", "value": 500, "unit": "USD",
            "financial_period": "2026Q3",
            "source_url": "https://www.sec.gov/Archives/revenue.htm",
            "published_at": revenue_published,
        },
    )
    assert response.status_code == 200
    details = response.json()["details"]
    assert details["actual_published_at"] == eps_published
    assert details["eps_published_at"] == eps_published
    assert details["revenue_published_at"] == revenue_published
    assert details["eps_source_url"].endswith("example.htm")
    assert details["revenue_source_url"].endswith("revenue.htm")
    financial = decisions_client.get(base).json()["financial_analysis"]["surprises"]
    assert {item["metric"]: item["source_url"] for item in financial} == {
        "eps": "https://www.sec.gov/Archives/example.htm",
        "revenue": "https://www.sec.gov/Archives/revenue.htm",
    }
    page = decisions_client.get(f"/research?security_id={security_id}")
    assert page.status_code == 200
    assert "研究与行动工作台" in page.text
    assert "季度财报" in page.text
