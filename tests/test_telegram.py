from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from pydantic import SecretStr, ValidationError
from pytest import MonkeyPatch
from sqlalchemy.orm import Session

from trade_news_analysis import cli as cli_module
from trade_news_analysis import scheduler as scheduler_module
from trade_news_analysis.config import Settings
from trade_news_analysis.models import Article, Event, EventArticle
from trade_news_analysis.services import opportunities as opportunity_service
from trade_news_analysis.services.telegram import (
    MAX_MESSAGE_LENGTH,
    TELEGRAM_HELP_MESSAGE,
    TelegramClient,
    TelegramCommandService,
    TelegramDeliveryError,
    format_opportunity_digest,
)


class FakeResponse:
    def __init__(self, payload: Any):
        self.body = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def opportunity(index: int, *, name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or f"机会 {index}",
        "type": "产业主题",
        "primary_market": "US" if index % 2 else "A",
        "markets": ["A", "US"],
        "direction": "bullish",
        "score": 90 - index,
        "confidence": 0.8,
        "conflict": 0.1,
        "evidence_count": index,
        "as_of": datetime(2026, 8, 26, 0, tzinfo=UTC),
        "detail_url": f"/opportunities/theme?name={index}&horizon=5",
        "source_url": f"https://news.example/story?a={index}&b=2",
        "source_name": "News <Wire>",
        "source_title": f"Source story {index}",
    }


def test_digest_formats_top_five_escapes_html_and_adds_public_links() -> None:
    rows = [opportunity(1, name="机器人 <Alpha>")] + [opportunity(index) for index in range(2, 7)]

    message = format_opportunity_digest(
        rows,
        horizon=5,
        limit=5,
        timezone="Asia/Shanghai",
        public_base_url="https://research.example/app",
        now=datetime(2026, 8, 26, 1, tzinfo=UTC),
    )

    assert "机器人 &lt;Alpha&gt;" in message
    assert "https://research.example/app/opportunities/theme?name=1&amp;horizon=5" in message
    assert "https://news.example/story?a=1&amp;b=2" in message
    assert "News &lt;Wire&gt; · Source story 1" in message
    assert "5. 机会 5" in message
    assert "机会 6" not in message
    assert "数据时间：2026-08-26 08:00" in message
    assert len(message) <= MAX_MESSAGE_LENGTH


def test_digest_sends_explicit_empty_state_without_local_link() -> None:
    message = format_opportunity_digest(
        [],
        horizon=5,
        limit=5,
        timezone="Asia/Shanghai",
        public_base_url=None,
        now=datetime(2026, 8, 26, 1, tzinfo=UTC),
    )

    assert "当前没有已验证趋势" in message
    assert "href=" not in message


def test_telegram_client_retries_429_and_honors_retry_after() -> None:
    calls: list[Request] = []
    sleeps: list[float] = []

    def opener(request: Request, **_kwargs: Any) -> FakeResponse:
        calls.append(request)
        if len(calls) == 1:
            body = BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 3},
                    }
                ).encode()
            )
            raise HTTPError(request.full_url, 429, "rate limited", Message(), body)
        return FakeResponse({"ok": True, "result": {"message_id": 42}})

    client = TelegramClient("secret-token", 5, opener=opener, sleeper=sleeps.append)
    result = client.send_message("123", "hello")

    assert result["message_id"] == 42
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_telegram_client_does_not_retry_400_or_leak_token() -> None:
    calls = 0

    def opener(request: Request, **_kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        body = BytesIO(
            json.dumps(
                {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            ).encode()
        )
        raise HTTPError(request.full_url, 400, "bad request", Message(), body)

    client = TelegramClient("secret-token", 5, opener=opener, sleeper=lambda _value: None)
    with pytest.raises(TelegramDeliveryError) as exc_info:
        client.send_message("123", "hello")

    assert calls == 1
    assert "chat not found" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


def test_telegram_client_rejects_invalid_json() -> None:
    class InvalidResponse(FakeResponse):
        def read(self) -> bytes:
            return b"not-json"

    client = TelegramClient(
        "secret-token",
        5,
        opener=lambda *_args, **_kwargs: InvalidResponse({}),
        sleeper=lambda _value: None,
    )
    with pytest.raises(TelegramDeliveryError, match="无效 JSON"):
        client.list_chats()


def test_telegram_client_get_updates_uses_persisted_offset() -> None:
    requests: list[Request] = []

    def opener(request: Request, **_kwargs: Any) -> FakeResponse:
        requests.append(request)
        return FakeResponse({"ok": True, "result": []})

    client = TelegramClient("secret-token", 5, opener=opener)

    assert client.get_updates(42) == []
    request_data = requests[0].data
    assert isinstance(request_data, bytes)
    payload = json.loads(request_data.decode())
    assert payload == {
        "limit": 100,
        "timeout": 0,
        "allowed_updates": ["message"],
        "offset": 42,
    }


def test_telegram_enabled_requires_token_and_chat_id() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN.*TELEGRAM_CHAT_ID"):
        Settings(telegram_enabled=True, telegram_bot_token=None, telegram_chat_id=None)


def test_digest_horizon_accepts_environment_string() -> None:
    settings = Settings.model_validate({"telegram_digest_horizon": "5"})

    assert settings.telegram_digest_horizon == 5


def test_opportunity_uses_latest_supporting_article(session: Session) -> None:
    event = Event(event_key="source-event", title="Source event", status="complete")
    old_article = Article(
        fingerprint="old-source",
        canonical_url="https://news.example/old",
        source="Old Wire",
        title="Old story",
        summary="",
        published_at=datetime(2026, 8, 25, tzinfo=UTC),
        fetched_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        story_cluster_id="source-event",
    )
    new_article = Article(
        fingerprint="new-source",
        canonical_url="https://news.example/new",
        source="New Wire",
        title="New story",
        summary="",
        published_at=datetime(2026, 8, 26, tzinfo=UTC),
        fetched_at=datetime(2026, 8, 26, 1, tzinfo=UTC),
        story_cluster_id="source-event",
    )
    session.add_all([event, old_article, new_article])
    session.flush()
    session.add_all(
        [
            EventArticle(event_id=event.id, article_id=old_article.id),
            EventArticle(event_id=event.id, article_id=new_article.id),
        ]
    )
    session.commit()
    opportunities = [{"event_ids": [event.id]}]

    opportunity_service.attach_latest_source_links(session, opportunities)

    assert opportunities[0]["source_url"] == "https://news.example/new"
    assert opportunities[0]["source_name"] == "New Wire"
    assert opportunities[0]["source_title"] == "New story"


class FakeScheduler:
    def __init__(self, timezone: str):
        self.timezone = timezone
        self.jobs: list[tuple[str, dict[str, Any]]] = []
        self.started = False

    def add_job(self, _func: Any, trigger: str, **kwargs: Any) -> None:
        self.jobs.append((trigger, kwargs))

    def start(self) -> None:
        self.started = True


class FakeCoordinator:
    def submit_pipeline(self, _trigger: str) -> int:
        return 1

    def submit_evaluation(self) -> None:
        return None

    def submit_telegram_digest(self) -> None:
        return None

    def poll_telegram_commands(self) -> int:
        return 0


def test_scheduler_registers_weekday_shanghai_digest(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", FakeScheduler)
    settings = Settings(
        scheduler_enabled=True,
        telegram_enabled=True,
        telegram_bot_token=SecretStr("token"),
        telegram_chat_id="123",
    )
    coordinator: Any = FakeCoordinator()
    scheduler: Any = scheduler_module.start_scheduler(settings, coordinator)

    job = next(
        kwargs
        for _, kwargs in scheduler.jobs
        if kwargs["id"] == "telegram-opportunity-digest"
    )
    assert job["day_of_week"] == "mon-fri"
    assert job["hour"] == 8
    assert job["minute"] == 30
    assert job["timezone"] == "Asia/Shanghai"
    polling_job = next(
        kwargs
        for _, kwargs in scheduler.jobs
        if kwargs["id"] == "telegram-command-polling"
    )
    assert polling_job["seconds"] == 5
    assert polling_job["max_instances"] == 1
    assert scheduler.started is True


def test_scheduler_omits_digest_when_telegram_is_disabled(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", FakeScheduler)
    coordinator: Any = FakeCoordinator()
    scheduler: Any = scheduler_module.start_scheduler(
        Settings(telegram_enabled=False), coordinator
    )

    assert all(
        kwargs["id"] != "telegram-opportunity-digest" for _, kwargs in scheduler.jobs
    )
    assert all(
        kwargs["id"] != "telegram-command-polling" for _, kwargs in scheduler.jobs
    )


def test_telegram_commands_send_digest_help_and_persist_offset(tmp_path: Path) -> None:
    class FakeCommandClient:
        def __init__(self, updates: list[dict[str, Any]]):
            self.updates = updates
            self.offsets: list[int | None] = []
            self.sent: list[tuple[str, str]] = []

        def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
            self.offsets.append(offset)
            result = self.updates
            self.updates = []
            return result

        def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
            self.sent.append((chat_id, text))
            return {"message_id": len(self.sent)}

    class FakeDigestService:
        def render_digest(self) -> str:
            return "<b>digest</b>"

    updates = [
        {
            "update_id": 12,
            "message": {"chat": {"id": 999}, "text": "/digest"},
        },
        {
            "update_id": 10,
            "message": {"chat": {"id": 123}, "text": "/digest@TestBot now"},
        },
        {
            "update_id": 11,
            "message": {"chat": {"id": 123}, "text": "/help"},
        },
    ]
    client = FakeCommandClient(updates)
    settings = Settings(
        telegram_bot_token=SecretStr("token"),
        telegram_chat_id="123",
        telegram_update_offset_path=tmp_path / "telegram-offset",
    )
    digest_service: Any = FakeDigestService()
    command_client: Any = client
    service = TelegramCommandService(settings, digest_service, command_client)

    assert service.poll() == 2
    assert client.sent == [
        ("123", "<b>digest</b>"),
        ("123", TELEGRAM_HELP_MESSAGE),
    ]
    assert settings.telegram_update_offset_path.read_text() == "13"

    restarted_client = FakeCommandClient([])
    restarted_command_client: Any = restarted_client
    restarted_service = TelegramCommandService(
        settings,
        digest_service,
        restarted_command_client,
    )
    assert restarted_service.poll() == 0
    assert restarted_client.offsets == [13]


def test_cli_lists_chats_without_printing_token(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def list_chats(self) -> list[dict[str, Any]]:
            return [{"id": "123", "type": "private", "name": "Alex", "username": "alex"}]

    settings = Settings(telegram_bot_token=SecretStr("secret-token"))
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "telegram_client_from_settings", lambda _settings: FakeClient())
    monkeypatch.setattr(sys, "argv", ["trade-news", "telegram-chats"])

    cli_module.main()

    captured = capsys.readouterr()
    assert "123\tprivate\tAlex @alex" in captured.out
    assert "secret-token" not in captured.out + captured.err


def test_cli_digest_dry_run_does_not_require_telegram_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cli-dry-run.db'}",
        scheduler_enabled=False,
        seed_watchlist="",
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["trade-news", "telegram-digest", "--dry-run"])

    cli_module.main()

    captured = capsys.readouterr()
    assert "跨市场机会日报" in captured.out
    assert "当前没有已验证趋势" in captured.out


def test_cli_digest_failure_returns_nonzero(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingDigestService:
        def __init__(self, _factory: Any, _settings: Settings):
            pass

        def send_digest(self) -> dict[str, Any]:
            raise TelegramDeliveryError("chat not found")

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cli-send.db'}",
        scheduler_enabled=False,
        seed_watchlist="",
        telegram_bot_token=SecretStr("secret-token"),
        telegram_chat_id="123",
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "TelegramDigestService", FailingDigestService)
    monkeypatch.setattr(sys, "argv", ["trade-news", "telegram-digest"])

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "chat not found" in captured.err
    assert "secret-token" not in captured.out + captured.err
