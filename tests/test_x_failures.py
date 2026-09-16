from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from trade_news_analysis import cli
from trade_news_analysis.config import DEFAULT_X_ACCOUNTS, Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import SourceHealth, XAccount, XPost
from trade_news_analysis.schemas import XPostScreeningPayload
from trade_news_analysis.services.ingestion import IngestionService
from trade_news_analysis.services.x_posts import BrowserPost, XIngestionService, XPostScreener


class AccountFetcher:
    def __init__(self, successful_handle: str | None = None) -> None:
        self.successful_handle = successful_handle
        self.calls: list[str] = []

    def fetch(self, handle: str) -> list[BrowserPost]:
        self.calls.append(handle)
        if handle != self.successful_handle:
            raise RuntimeError("X 网络连接失败，请检查代理。")
        return [
            BrowserPost(
                post_id="20001",
                url=f"https://x.com/{handle}/status/20001",
                post_type="original",
                text="Example Corp may benefit from stronger demand.",
                published_at=datetime.now(UTC),
            )
        ]


def ingestion_service(
    session_factory: SessionFactory, settings: Settings, fetcher: AccountFetcher
) -> XIngestionService:
    screening = XPostScreeningPayload(
        classification="opinion",
        claim_summary="作者认为需求增长可能使公司受益。",
        stance="bullish",
        horizon="1w",
        market_relevance=3,
        specificity=3,
        incrementality=3,
        rationale="作者观点，尚需事实验证。",
    )
    ingestion = IngestionService(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
        master_factory=lambda _settings: None,
    )
    return XIngestionService(
        session_factory,
        settings,
        ingestion,
        fetcher=fetcher,
        screener=XPostScreener(
            settings, completion=lambda _system, _prompt: screening.model_dump_json()
        ),
    )


def test_all_accounts_fail_after_health_is_committed(
    session_factory: SessionFactory, settings: Settings
) -> None:
    fetcher = AccountFetcher()
    service = ingestion_service(session_factory, settings, fetcher)
    count = len(DEFAULT_X_ACCOUNTS)

    for attempt in (1, 2):
        with pytest.raises(RuntimeError, match=f"{count}/{count} 个账号失败"):
            service.execute()

        with session_factory() as session:
            health = session.scalars(
                select(SourceHealth).where(SourceHealth.capability == "x_posts")
            ).all()
            assert len(health) == count
            assert all(item.consecutive_failures == attempt for item in health)
            assert all(item.last_attempt_at is not None for item in health)
            assert all(item.last_success_at is None for item in health)
            assert all("检查代理" in (item.last_error or "") for item in health)
            assert all(item.items_last_run == 0 for item in health)

    assert fetcher.calls == list(DEFAULT_X_ACCOUNTS) * 2


@pytest.mark.parametrize("successful_handle", [DEFAULT_X_ACCOUNTS[0], DEFAULT_X_ACCOUNTS[-1]])
def test_partial_failure_keeps_successful_posts_and_health(
    session_factory: SessionFactory, settings: Settings, successful_handle: str
) -> None:
    fetcher = AccountFetcher(successful_handle)
    service = ingestion_service(session_factory, settings, fetcher)

    assert service.execute() == set()
    assert fetcher.calls == list(DEFAULT_X_ACCOUNTS)

    with session_factory() as session:
        posts = session.scalars(select(XPost)).all()
        assert len(posts) == 1
        assert posts[0].post_id == "20001"
        assert posts[0].screening_status == "context"
        health = session.scalars(
            select(SourceHealth).where(SourceHealth.capability == "x_posts")
        ).all()
        for item in health:
            if item.source == f"X:@{successful_handle}":
                assert item.last_success_at is not None
                assert item.last_error is None
                assert item.consecutive_failures == 0
                assert item.items_last_run == 1
            else:
                assert item.last_success_at is None
                assert item.last_error is not None
                assert item.consecutive_failures == 1
                assert item.items_last_run == 0


def test_no_enabled_accounts_reports_configuration_error(
    session_factory: SessionFactory, settings: Settings
) -> None:
    with session_factory() as session:
        session.execute(update(XAccount).values(active=False))
        session.commit()
    fetcher = AccountFetcher()

    with pytest.raises(RuntimeError, match="没有启用的博主账号"):
        ingestion_service(session_factory, settings, fetcher).execute()

    assert fetcher.calls == []


def configure_cli(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[Mock, Mock]:
    engine = Mock()
    coordinator = Mock()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(Settings, "ensure_local_directories", lambda _settings: None)
    monkeypatch.setattr(cli, "build_engine", lambda _url: engine)
    monkeypatch.setattr(cli, "initialize_database", Mock())
    monkeypatch.setattr(cli, "build_session_factory", Mock())
    monkeypatch.setattr(cli, "PipelineCoordinator", lambda _factory, _settings: coordinator)
    monkeypatch.setattr(sys, "argv", ["trade-news", "x-ingest"])
    return engine, coordinator


@pytest.mark.parametrize("failure_stage", ["submit", "result"])
def test_cli_failure_returns_nonzero_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
) -> None:
    engine, coordinator = configure_cli(monkeypatch, settings)
    failure = RuntimeError("所有博主账号采集失败")
    if failure_stage == "submit":
        coordinator.submit_x_ingestion.side_effect = failure
    else:
        coordinator.submit_x_ingestion.return_value.result.side_effect = failure

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "所有博主账号采集失败" in captured.err
    coordinator.shutdown.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_cli_success_reports_account_results_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, coordinator = configure_cli(monkeypatch, settings)
    coordinator.submit_x_ingestion.return_value.result.return_value = set()

    cli.main()

    captured = capsys.readouterr()
    assert "X 采集流程已结束" in captured.out
    assert "确认各账号结果" in captured.out
    assert captured.err == ""
    coordinator.shutdown.assert_called_once_with()
    engine.dispose.assert_called_once_with()
