"""Local CLI entry point."""

from __future__ import annotations

import argparse
import time

import uvicorn

from .config import get_settings
from .db import build_engine, build_session_factory, initialize_database
from .services.coordinator import PipelineCoordinator


def main() -> None:
    parser = argparse.ArgumentParser(prog="trade-news")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动本地 API 和网页")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("ingest", help="同步执行一次采集、分析和验证")
    args = parser.parse_args()

    if args.command == "serve":
        uvicorn.run("trade_news_analysis.main:app", host=args.host, port=args.port, reload=False)
        return

    settings = get_settings()
    settings.ensure_local_directories()
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    factory = build_session_factory(engine)
    coordinator = PipelineCoordinator(factory, settings)
    run_id = coordinator.submit_pipeline("cli")
    while coordinator.busy:
        time.sleep(0.1)
    coordinator.shutdown()
    print(f"ingestion run {run_id} completed")
