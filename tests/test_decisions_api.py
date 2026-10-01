from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import (
    Event,
    EventSecurityImpact,
    EventTheme,
    Security,
    SecuritySignalSnapshot,
    Theme,
    XAccount,
    XPost,
)
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.scoring import rebuild_signal_snapshots

from .test_api import EmptyProvider, FakeFundamentalProvider


@pytest.fixture
def decisions_client(settings: Settings, session_factory: SessionFactory) -> Iterator[TestClient]:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
        evaluator=OutcomeEvaluator(provider=EmptyProvider()),
    )
    app = create_app(
        settings,
        session_factory,
        coordinator,
        fundamental_provider=FakeFundamentalProvider(),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def decision_security(session_factory: SessionFactory) -> tuple[int, int]:
    now = datetime.now(UTC)
    with session_factory() as session:
        security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
        assert security is not None
        event = Event(
            event_key="decision-api-order",
            title="公司公告新增订单",
            status="complete",
            demand_status="observed",
            occurred_at=now,
        )
        session.add(event)
        session.flush()
        impact = EventSecurityImpact(
            security_id=security.id,
            event_id=event.id,
            status="complete",
            is_current=True,
            opportunity_score=80,
            impacts={
                str(horizon): {
                    "direction": "bullish",
                    "confidence": confidence,
                    "reason": "订单将在本季度开始交付",
                }
                for horizon, confidence in ((1, 0.3), (5, 0.8), (20, 0.7))
            },
            catalysts=["订单开始交付"],
            risks=["交付延期"],
            falsifiers=["公司公告取消订单"],
        )
        session.add(impact)
        session.commit()
        rebuild_signal_snapshots(session, now)
        return security.id, impact.id


def ready_watchlist(client: TestClient, security_id: int, holding_status: str) -> dict[str, Any]:
    response = client.put(
        "/api/v1/watchlist",
        json={"items": [{"security_id": security_id, "holding_status": holding_status}]},
    )
    assert response.status_code == 200
    response = client.put(
        f"/api/v1/securities/{security_id}/pe-analysis",
        json={
            "overrides": {
                "fiscal_year": datetime.now(UTC).year - 1,
                "price": 10,
                "shares_outstanding": 100,
                "revenue": 1000,
                "net_income": 100,
            },
            "assumptions": [
                {
                    "year_offset": offset,
                    "revenue_growth": 0.1,
                    "net_income_growth": 0.1,
                    "pe_low": 15,
                    "pe_high": 20,
                }
                for offset in range(1, 5)
            ],
        },
    )
    assert response.status_code == 200
    summary: dict[str, Any] = response.json()["summary"]
    assert summary["status"] == "ready"
    assert summary["valuation_year"] == datetime.now(UTC).year
    assert summary["price_as_of"] is not None
    assert summary["price_provenance"] == "manual"
    return summary


def test_action_plan_exposes_neutral_event_checks_and_horizon_on_page(
    decisions_client: TestClient, decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    security_id, impact_id = decision_security
    ready_watchlist(decisions_client, security_id, "long")
    with session_factory() as session:
        impact = session.get(EventSecurityImpact, impact_id)
        assert impact is not None
        impact.impacts = {
            str(h): {"direction": "neutral", "confidence": 0.3, "reason": "等待交付"}
            for h in (1, 5, 20)
        }
        event_id = impact.event_id
        session.commit()
        rebuild_signal_snapshots(session)
    response = decisions_client.get(f"/api/v1/securities/{security_id}?horizon=20")
    action = response.json()["judgment"]["action"]
    assert action["code"] == "review"
    assert action["plan"]["horizon"] == 20
    assert any(task["source_id"] == event_id for task in action["plan"]["tasks"])
    page = decisions_client.get(f"/securities/{security_id}?horizon=20")
    assert page.status_code == 200
    assert "现在具体做什么 · 20 日评估" in page.text
    assert "订单开始交付" in page.text
    assert "公司公告取消订单" in page.text
    assert f'/events/{event_id}' in page.text
    assert 'id="action-plan"' in page.text


@pytest.mark.parametrize("holding_status", ["flat", "long"])
@pytest.mark.parametrize("horizon", [1, 5, 20])
def test_decisions_match_between_watchlist_security_api_and_pages(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
    holding_status: str,
    horizon: int,
) -> None:
    security_id, _ = decision_security
    ready_watchlist(decisions_client, security_id, holding_status)
    watchlist_response = decisions_client.get(f"/api/v1/watchlist?horizon={horizon}")
    detail_response = decisions_client.get(
        f"/api/v1/securities/{security_id}?horizon={horizon}"
    )
    assert watchlist_response.status_code == detail_response.status_code == 200
    watchlist = watchlist_response.json()[0]
    detail = detail_response.json()
    expected_code = (
        ("wait" if holding_status == "flat" else "review")
        if horizon == 1
        else ("buy_candidate" if holding_status == "flat" else "hold")
    )
    for result in (watchlist, detail):
        assert result["holding_status"] == holding_status
        assert set(result["signals"]) == set(result["judgments"]) == {"1", "5", "20"}
        assert result["signal"]["horizon"] == horizon
        assert result["signal"] == result["signals"][str(horizon)]
        assert result["judgment"] == result["judgments"][str(horizon)]
        assert result["judgment"]["action"]["code"] == expected_code
        assert result["judgment"]["action"]["holding_status"] == holding_status
    assert watchlist["signals"] == detail["signals"]

    dashboard = decisions_client.get(f"/?horizon={horizon}")
    security_page = decisions_client.get(f"/securities/{security_id}?horizon={horizon}")
    assert dashboard.status_code == security_page.status_code == 200
    page_watchlist = dashboard.context["watchlist"][0]
    page_security = security_page.context["security"]
    for page_result in (page_watchlist, page_security):
        assert jsonable_encoder(page_result["signal"]) == detail["signal"]
        assert page_result["judgment"]["action"]["code"] == expected_code
        assert page_result["holding_status"] == holding_status
    label = detail["judgment"]["action"]["label"]
    assert label in dashboard.text
    assert label in security_page.text


def test_default_five_day_buy_candidate_and_legacy_holding_updates(
    decisions_client: TestClient, decision_security: tuple[int, int]
) -> None:
    security_id, _ = decision_security
    ready_watchlist(decisions_client, security_id, "flat")
    detail = decisions_client.get(f"/api/v1/securities/{security_id}").json()
    watchlist = decisions_client.get("/api/v1/watchlist").json()[0]
    assert detail["signal"]["horizon"] == watchlist["signal"]["horizon"] == 5
    assert detail["judgment"]["action"]["code"] == "buy_candidate"
    assert detail["judgment"]["action"]["blockers"] == []

    response = decisions_client.put(
        "/api/v1/watchlist",
        json={"items": [{"security_id": security_id, "holding_status": "long"}]},
    )
    assert response.status_code == 200
    assert response.json()[0]["judgment"]["action"]["code"] == "hold"
    legacy_response = decisions_client.put(
        "/api/v1/watchlist", json={"items": [{"security_id": security_id, "active": False}]}
    )
    assert legacy_response.status_code == 200
    assert legacy_response.json()[0]["holding_status"] == "long"
    assert legacy_response.json()[0]["active"] is False

    invalid_response = decisions_client.put(
        "/api/v1/watchlist",
        json={"items": [{"security_id": security_id, "holding_status": "invalid"}]},
    )
    assert invalid_response.status_code == 422
    assert decisions_client.get("/api/v1/watchlist").json()[0]["holding_status"] == "long"


def test_revoked_evidence_immediately_blocks_buy_candidate(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    security_id, impact_id = decision_security
    ready_watchlist(decisions_client, security_id, "flat")
    endpoint = f"/api/v1/securities/{security_id}"
    assert decisions_client.get(endpoint).json()["judgment"]["action"]["code"] == "buy_candidate"
    with session_factory() as session:
        impact = session.get(EventSecurityImpact, impact_id)
        assert impact is not None
        impact.is_current = False
        session.commit()

    for result in (
        decisions_client.get(endpoint).json(),
        decisions_client.get("/api/v1/watchlist").json()[0],
    ):
        assert result["signal"] is None
        assert result["signals"] == {"1": None, "5": None, "20": None}
        assert result["invalidated_horizons"] == [1, 5, 20]
        assert result["judgment"]["action"]["code"] == "wait"
        assert result["judgment"]["action"]["blockers"]
        assert result["event_checks"] == []


def test_x_opinion_is_linked_and_displayed_without_changing_formal_score(
    decisions_client: TestClient,
    decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    security_id, _ = decision_security
    ready_watchlist(decisions_client, security_id, "flat")
    endpoint = f"/api/v1/securities/{security_id}"
    before = decisions_client.get(endpoint).json()
    url = "https://x.com/decision_test/status/9001"
    with session_factory() as session:
        account = XAccount(handle="decision_test", account_type="commentator")
        session.add(account)
        session.flush()
        post = XPost(
            account_id=account.id,
            post_id="9001",
            url=url,
            post_type="original",
            text="我认为 AAPL 交付进度可能不及预期。",
            published_at=datetime.now(UTC) - timedelta(minutes=15),
            screening_status="context",
            screening={
                "classification": "opinion",
                "entities": ["AAPL"],
                "themes": [],
                "stance": "bearish",
                "claim_summary": "作者担忧交付进度",
                "factual_claims": ["作者称交付可能延期"],
                "verification_needs": ["核对公司正式交付公告"],
            },
        )
        session.add(post)
        session.commit()
        post_id = post.id

    after = decisions_client.get(endpoint).json()
    social = after["social"]
    assert social["post_count"] == social["direct_post_count"] == 1
    assert social["stance"] == "bearish"
    assert social["formal_score_impact"] == 0
    assert social["posts"][0]["id"] == post_id
    assert social["posts"][0]["url"] == url
    assert social["posts"][0]["match_basis"] == "entity"
    assert social["posts"][0]["is_confirmed_fact"] is False
    assert social["factual_claims"][0]["status"] == "unverified"
    assert social["verification_needs"] == ["核对公司正式交付公告"]
    assert after["signals"] == before["signals"]
    assert after["judgment"]["action"]["code"] == "buy_candidate"
    assert "核对公司正式交付公告" in after["judgment"]["action"]["next_steps"]
    watchlist_social = decisions_client.get("/api/v1/watchlist").json()[0]["social"]
    assert {key: value for key, value in watchlist_social.items() if key != "as_of"} == {
        key: value for key, value in social.items() if key != "as_of"
    }

    page = decisions_client.get(f"/securities/{security_id}")
    assert page.status_code == 200
    assert url in page.text
    assert "待核实" in page.text
    assert "核对公司正式交付公告" in page.text
    with session_factory() as session:
        stored_post = session.get(XPost, post_id)
        assert stored_post is not None
        assert stored_post.promoted_article_id is None
        assert stored_post.related_event_id is None
        assert session.scalar(select(func.count()).select_from(SecuritySignalSnapshot)) == 3


def test_theme_search_limits_preserve_counts_and_bound_dashboard_options(
    decisions_client: TestClient, session_factory: SessionFactory
) -> None:
    with session_factory() as session:
        themes = [
            Theme(slug=f"integration-theme-{index:02d}", name=f"集成主题{index:02d}")
            for index in range(35)
        ]
        events = [
            Event(event_key=f"theme-search-{index}", title=f"主题相关事件{index}")
            for index in range(2)
        ]
        session.add_all([*themes, *events])
        session.flush()
        counted_theme_id = themes[0].id
        session.add_all(
            [EventTheme(theme_id=counted_theme_id, event_id=event.id) for event in events]
        )
        session.commit()

    response = decisions_client.get("/api/v1/themes")
    assert response.status_code == 200
    all_themes = response.json()
    assert len(all_themes) == 35
    assert next(item for item in all_themes if item["id"] == counted_theme_id)["event_count"] == 2
    assert all(item["event_count"] == 0 for item in all_themes if item["id"] != counted_theme_id)

    by_name = decisions_client.get("/api/v1/themes", params={"q": "集成主题03"})
    assert by_name.status_code == 200
    assert [item["slug"] for item in by_name.json()] == ["integration-theme-03"]
    by_slug = decisions_client.get(
        "/api/v1/themes", params={"q": "integration-theme-0", "limit": 5}
    )
    assert by_slug.status_code == 200
    assert len(by_slug.json()) == 5
    assert by_slug.json()[0]["event_count"] == 2
    assert decisions_client.get("/api/v1/themes", params={"q": "不存在的主题"}).json() == []
    for limit in (0, -1, 501):
        assert decisions_client.get("/api/v1/themes", params={"limit": limit}).status_code == 422
    assert len(decisions_client.get("/api/v1/themes", params={"limit": 1}).json()) == 1
    assert len(decisions_client.get("/api/v1/themes", params={"limit": 500}).json()) == 35

    dashboard = decisions_client.get("/")
    assert dashboard.status_code == 200
    assert len(dashboard.context["themes"]) == 30
    assert 'id="theme-search"' in dashboard.text
    options = re.search(r'<datalist id="theme-options">(.*?)</datalist>', dashboard.text, re.S)
    assert options is not None
    assert options.group(1).count("<option ") == 30


def test_x_single_post_links_find_older_posts_and_respect_filters(
    decisions_client: TestClient, session_factory: SessionFactory
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        original_author = XAccount(handle="original_test", account_type="commentator")
        newer_author = XAccount(handle="newer_test", account_type="commentator")
        session.add_all([original_author, newer_author])
        session.flush()
        original = XPost(
            account_id=original_author.id,
            post_id="5000",
            url="https://x.com/original_test/status/5000",
            post_type="original",
            text="这是一条需要从研究页面直达的较旧线索。",
            published_at=now - timedelta(days=10),
            screening_status="context",
            screening={"classification": "opinion"},
        )
        session.add(original)
        ignored_posts = [
            XPost(
                account_id=original_author.id,
                post_id=f"ignored-{status}",
                url=f"https://x.com/original_test/status/ignored-{status}",
                post_type="original",
                text="已忽略的历史线索",
                published_at=now - timedelta(days=20),
                screening_status=status,
                screening={"classification": "opinion"},
            )
            for status in ("ignore", "ignored")
        ]
        session.add_all(ignored_posts)
        session.add_all(
            [
                XPost(
                    account_id=newer_author.id,
                    post_id=str(6000 + index),
                    url=f"https://x.com/newer_test/status/{6000 + index}",
                    post_type="original",
                    text=f"较新的动态 {index}",
                    published_at=now - timedelta(minutes=index),
                    screening_status="review",
                    screening={"classification": "fact"},
                )
                for index in range(201)
            ]
        )
        session.commit()
        original_id = original.id
        original_account_id = original_author.id
        newer_account_id = newer_author.id
        ignored_ids = {post.id for post in ignored_posts}

    recent = decisions_client.get("/api/v1/x/posts", params={"limit": 200})
    assert recent.status_code == 200
    assert len(recent.json()) == 200
    assert original_id not in {item["id"] for item in recent.json()}
    direct = decisions_client.get(
        "/api/v1/x/posts", params={"post_id": original_id, "limit": 1}
    )
    assert direct.status_code == 200
    assert [item["id"] for item in direct.json()] == [original_id]
    matching = decisions_client.get(
        "/api/v1/x/posts",
        params={
            "post_id": original_id,
            "account_id": original_account_id,
            "screening_status": "context",
            "classification": "opinion",
        },
    )
    assert [item["id"] for item in matching.json()] == [original_id]
    for filters in (
        {"account_id": newer_account_id},
        {"screening_status": "review"},
        {"classification": "fact"},
    ):
        filtered = decisions_client.get(
            "/api/v1/x/posts", params={"post_id": original_id, **filters}
        )
        assert filtered.status_code == 200
        assert filtered.json() == []

    page = decisions_client.get("/x", params={"post_id": original_id})
    assert page.status_code == 200
    assert [item["id"] for item in page.context["posts"]] == [original_id]
    assert f'id="post-{original_id}"' in page.text
    assert "https://x.com/original_test/status/5000" in page.text
    missing_id = original_id + 10000
    missing_page = decisions_client.get("/x", params={"post_id": missing_id})
    assert missing_page.status_code == 200
    assert missing_page.context["posts"] == []
    assert "尚无帖子" in missing_page.text
    assert decisions_client.get("/api/v1/x/posts", params={"post_id": missing_id}).json() == []

    all_accounts = decisions_client.get(
        "/x", params={"account_id": "", "screening_status": "review"}
    )
    assert all_accounts.status_code == 200
    assert len(all_accounts.context["posts"]) == 200
    assert all_accounts.context["selected_account_id"] is None
    assert all(post["screening_status"] == "review" for post in all_accounts.context["posts"])
    assert decisions_client.get("/x", params={"account_id": "invalid"}).status_code == 422
    for screening_status in ("ignore", "ignored"):
        ignored = decisions_client.get(
            "/api/v1/x/posts", params={"screening_status": screening_status}
        )
        assert ignored.status_code == 200
        assert {post["id"] for post in ignored.json()} == ignored_ids
    ignored_page = decisions_client.get(
        "/x", params={"account_id": "", "screening_status": "ignored"}
    )
    assert ignored_page.status_code == 200
    assert {post["id"] for post in ignored_page.context["posts"]} == ignored_ids
