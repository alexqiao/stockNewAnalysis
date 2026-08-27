"""Local CLI entry point."""

from __future__ import annotations

import argparse
import sys
import time

import uvicorn

from .config import get_settings
from .db import build_engine, build_session_factory, initialize_database
from .services.coordinator import PipelineCoordinator
from .services.telegram import TelegramDigestService, telegram_client_from_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="trade-news")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动本地 API 和网页")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("ingest", help="同步执行一次采集、分析和验证")
    subparsers.add_parser("telegram-chats", help="列出最近与 Bot 交互的 Telegram 会话")
    telegram_digest = subparsers.add_parser(
        "telegram-digest", help="生成并发送跨市场机会日报"
    )
    telegram_digest.add_argument(
        "--dry-run", action="store_true", help="只在终端预览，不发送消息"
    )
    args = parser.parse_args()

    if args.command == "serve":
        uvicorn.run("trade_news_analysis.main:app", host=args.host, port=args.port, reload=False)
        return

    settings = get_settings()
    if args.command == "telegram-chats":
        try:
            chats = telegram_client_from_settings(settings).list_chats()
        except Exception as exc:
            print(f"无法读取 Telegram 会话：{type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        if not chats:
            print("未找到会话。请先打开 Bot 并发送 /start，然后重试。")
            return
        for chat in chats:
            username = f" @{chat['username']}" if chat["username"] else ""
            print(f"{chat['id']}\t{chat['type']}\t{chat['name']}{username}")
        return

    settings.ensure_local_directories()
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    factory = build_session_factory(engine)

    if args.command == "telegram-digest":
        service = TelegramDigestService(factory, settings)
        try:
            if args.dry_run:
                print(service.render_digest())
            else:
                result = service.send_digest()
                print(f"Telegram 日报已发送，message_id={result.get('message_id', '-')}")
        except Exception as exc:
            print(f"Telegram 日报失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        finally:
            engine.dispose()
        return

    coordinator = PipelineCoordinator(factory, settings)
    run_id = coordinator.submit_pipeline("cli")
    while coordinator.busy:
        time.sleep(0.1)
    coordinator.shutdown()
    engine.dispose()
    print(f"ingestion run {run_id} completed")
