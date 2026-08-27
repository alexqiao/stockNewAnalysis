"""Telegram Bot API client and cross-market opportunity digest."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import SessionFactory
from .opportunities import dashboard_opportunities

MAX_MESSAGE_LENGTH = 4096
MAX_ATTEMPTS = 3
DIRECTION_LABELS = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
TELEGRAM_HELP_MESSAGE = (
    "<b>可用指令</b>\n"
    "/digest — 立即发送跨市场机会日报\n"
    "/help — 查看指令说明"
)


class TelegramDeliveryError(RuntimeError):
    """A sanitized Telegram delivery failure that never includes the bot token."""


class _TelegramRequestError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: float = 0.0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class TelegramClient:
    def __init__(
        self,
        token: str,
        timeout: float,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("TELEGRAM_BOT_TOKEN 未配置")
        self._base_url = f"https://api.telegram.org/bot{normalized_token}/"
        self._timeout = timeout
        self._opener = opener
        self._sleeper = sleeper

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        if not chat_id.strip():
            raise ValueError("TELEGRAM_CHAT_ID 未配置")
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return result if isinstance(result, dict) else {}

    def list_chats(self) -> list[dict[str, Any]]:
        result = self._call("getUpdates", {})
        if not isinstance(result, list):
            return []
        chats: dict[str, dict[str, Any]] = {}
        for update in result:
            if not isinstance(update, dict):
                continue
            candidates = [
                update.get("message"),
                update.get("edited_message"),
                update.get("channel_post"),
                update.get("edited_channel_post"),
            ]
            membership = update.get("my_chat_member")
            if isinstance(membership, dict):
                candidates.append({"chat": membership.get("chat")})
            for candidate in candidates:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("chat"), dict):
                    continue
                chat = candidate["chat"]
                chat_id = str(chat.get("id") or "").strip()
                if not chat_id:
                    continue
                title = str(chat.get("title") or "").strip()
                if not title:
                    title = " ".join(
                        part
                        for part in (
                            str(chat.get("first_name") or "").strip(),
                            str(chat.get("last_name") or "").strip(),
                        )
                        if part
                    )
                chats[chat_id] = {
                    "id": chat_id,
                    "type": str(chat.get("type") or "unknown"),
                    "name": title or str(chat.get("username") or "未命名会话"),
                    "username": str(chat.get("username") or ""),
                }
        return sorted(chats.values(), key=lambda item: (item["type"], item["id"]))

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "limit": 100,
            "timeout": 0,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        last_error = "Telegram 请求失败"
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._call_once(method, payload)
            except _TelegramRequestError as exc:
                last_error = str(exc)
                if not exc.retryable or attempt == MAX_ATTEMPTS - 1:
                    break
                delay = exc.retry_after if exc.retry_after > 0 else 2**attempt
                self._sleeper(min(delay, 30.0))
        raise TelegramDeliveryError(last_error)

    def _call_once(self, method: str, payload: dict[str, Any]) -> Any:
        request = Request(
            f"{self._base_url}{method}",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except HTTPError as exc:
            raw_body = exc.read()
            parsed = _decode_json(raw_body)
            description = _telegram_description(parsed, f"HTTP {exc.code}")
            raise _TelegramRequestError(
                f"Telegram API 返回 {exc.code}：{description}",
                retryable=exc.code == 429 or exc.code >= 500,
                retry_after=_retry_after(parsed),
            ) from None
        except (TimeoutError, URLError) as exc:
            raise _TelegramRequestError(
                f"Telegram 网络请求失败：{type(exc).__name__}", retryable=True
            ) from None

        parsed = _decode_json(raw_body)
        if not isinstance(parsed, dict):
            raise _TelegramRequestError("Telegram API 返回了无效 JSON", retryable=False)
        if parsed.get("ok") is not True:
            error_code = int(parsed.get("error_code") or 0)
            description = _telegram_description(parsed, "未知错误")
            raise _TelegramRequestError(
                f"Telegram API 返回 {error_code or '错误'}：{description}",
                retryable=error_code == 429 or error_code >= 500,
                retry_after=_retry_after(parsed),
            )
        return parsed.get("result")


def _decode_json(raw_body: bytes) -> Any:
    try:
        return json.loads(raw_body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _telegram_description(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    description = str(payload.get("description") or fallback).strip()
    return description[:300]


def _retry_after(payload: Any) -> float:
    if not isinstance(payload, dict) or not isinstance(payload.get("parameters"), dict):
        return 0.0
    try:
        return max(0.0, float(payload["parameters"].get("retry_after") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def format_opportunity_digest(
    opportunities: list[dict[str, Any]],
    *,
    horizon: int,
    limit: int,
    timezone: str,
    public_base_url: str | None,
    now: datetime | None = None,
) -> str:
    zone = ZoneInfo(timezone)
    sent_at = _aware_datetime(now or datetime.now(UTC)).astimezone(zone)
    visible = opportunities[:limit]
    as_of_values = [item.get("as_of") for item in visible]
    data_as_of = max(
        (_aware_datetime(item) for item in as_of_values if isinstance(item, datetime)),
        default=None,
    )
    header = (
        f"<b>跨市场机会日报 · {horizon} 日信号</b>\n"
        f"发送时间：{sent_at:%Y-%m-%d %H:%M} {escape(timezone)}"
    )
    blocks: list[str] = []
    if not visible:
        blocks.append("当前没有已验证趋势。")
    else:
        for index, item in enumerate(visible, 1):
            name = escape(str(item.get("name") or "未命名机会")[:180])
            item_type = escape(str(item.get("type") or "未知类型")[:60])
            primary_market = escape(str(item.get("primary_market") or "-")[:30])
            markets = " · ".join(str(value) for value in item.get("markets") or [])
            direction = DIRECTION_LABELS.get(str(item.get("direction")), "未知")
            block_lines = [
                f"<b>{index}. {name}</b> · {item_type}",
                f"主市场 {primary_market}｜覆盖 {escape(markets or '-')}｜{direction}",
                (
                    f"综合分 {float(item.get('score') or 0):.1f}｜"
                    f"置信度 {float(item.get('confidence') or 0) * 100:.0f}%｜"
                    f"冲突度 {float(item.get('conflict') or 0) * 100:.0f}%｜"
                    f"证据 {int(item.get('evidence_count') or 0)} 个标的"
                ),
            ]
            base_url = (public_base_url or "").strip()
            detail_url = str(item.get("detail_url") or "").strip()
            if base_url and detail_url:
                full_url = urljoin(f"{base_url.rstrip('/')}/", detail_url.lstrip("/"))
                block_lines.append(f'<a href="{escape(full_url, quote=True)}">查看详情</a>')
            source_url = str(item.get("source_url") or "").strip()
            if source_url:
                source_name = str(item.get("source_name") or "原始新闻").strip()[:80]
                source_title = str(item.get("source_title") or "").strip()[:140]
                source_label = (
                    f"{source_name} · {source_title}" if source_title else source_name
                )
                block_lines.append(
                    f'来源：<a href="{escape(source_url, quote=True)}">'
                    f"{escape(source_label)}</a>"
                )
            blocks.append("\n".join(block_lines))

    footer_lines = []
    if data_as_of is not None:
        footer_lines.append(f"数据时间：{data_as_of.astimezone(zone):%Y-%m-%d %H:%M}")
    footer_lines.append("仅供研究与教育使用，不构成投资建议。")
    footer = "\n".join(footer_lines)
    message = f"{header}\n\n" + "\n\n".join(blocks) + f"\n\n{footer}"
    if len(message) <= MAX_MESSAGE_LENGTH:
        return message

    compact_blocks: list[str] = []
    for block in blocks:
        candidate = f"{header}\n\n" + "\n\n".join([*compact_blocks, block]) + f"\n\n{footer}"
        if len(candidate) > MAX_MESSAGE_LENGTH:
            break
        compact_blocks.append(block)
    if not compact_blocks:
        compact_blocks.append("日报内容过长，请在网页中查看完整榜单。")
    return f"{header}\n\n" + "\n\n".join(compact_blocks) + f"\n\n{footer}"


class TelegramDigestService:
    def __init__(self, session_factory: SessionFactory, settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def render_digest(self, now: datetime | None = None) -> str:
        with self.session_factory() as session:
            opportunities = dashboard_opportunities(
                session,
                market=None,
                theme=None,
                horizon=self.settings.telegram_digest_horizon,
            )
        return format_opportunity_digest(
            opportunities,
            horizon=self.settings.telegram_digest_horizon,
            limit=self.settings.telegram_digest_limit,
            timezone=self.settings.telegram_digest_timezone,
            public_base_url=self.settings.public_base_url,
            now=now,
        )

    def send_digest(self, now: datetime | None = None) -> dict[str, Any]:
        client = telegram_client_from_settings(self.settings)
        chat_id = (self.settings.telegram_chat_id or "").strip()
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID 未配置")
        return client.send_message(chat_id, self.render_digest(now))


class TelegramCommandService:
    def __init__(
        self,
        settings: Settings,
        digest_service: TelegramDigestService,
        client: TelegramClient | None = None,
        offset_path: Path | None = None,
    ):
        chat_id = (settings.telegram_chat_id or "").strip()
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID 未配置")
        self.chat_id = chat_id
        self.digest_service = digest_service
        self.client = client or telegram_client_from_settings(settings)
        self.offset_path = offset_path or settings.telegram_update_offset_path
        self._offset = self._load_offset()
        self._lock = Lock()

    def poll(self) -> int:
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            handled = 0
            updates = sorted(
                self.client.get_updates(self._offset),
                key=lambda item: int(item.get("update_id") or 0),
            )
            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    continue
                if self._handle_update(update):
                    handled += 1
                self._offset = update_id + 1
                self._save_offset(self._offset)
            return handled
        finally:
            self._lock.release()

    def _handle_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            return False
        chat_id = str(message["chat"].get("id") or "").strip()
        if chat_id != self.chat_id:
            return False
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return False
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].casefold()
        if command == "/digest":
            self.client.send_message(self.chat_id, self.digest_service.render_digest())
            return True
        if command in {"/help", "/start"}:
            self.client.send_message(self.chat_id, TELEGRAM_HELP_MESSAGE)
            return True
        return False

    def _load_offset(self) -> int | None:
        try:
            value = int(self.offset_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError):
            return None
        return max(value, 0)

    def _save_offset(self, offset: int) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.offset_path.with_name(f"{self.offset_path.name}.tmp")
        temporary.write_text(str(offset), encoding="utf-8")
        temporary.replace(self.offset_path)


def telegram_client_from_settings(settings: Settings) -> TelegramClient:
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN 未配置")
    return TelegramClient(
        settings.telegram_bot_token.get_secret_value(),
        settings.request_timeout_seconds,
    )
