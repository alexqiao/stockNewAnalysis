"""Serialized research work with independent collection workers."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from ..config import Settings
from ..db import SessionFactory
from ..models import Event, EventSecurityImpact, IngestionRun, Security, Watchlist, utc_now
from .analysis import MAX_ANALYSIS_ATTEMPTS, EventAnalyzer
from .evaluation import OutcomeEvaluator
from .ingestion import IngestionService, SourceFactory
from .scoring import rebuild_signal_snapshots
from .telegram import TelegramCommandService, TelegramDigestService
from .x_posts import XIngestionService

logger = logging.getLogger(__name__)


class PipelineBusyError(RuntimeError):
    pass


@dataclass
class _DailyBarJob:
    run_id: int
    requested_ids: dict[int, bool] = field(default_factory=dict)
    pending_ids: dict[int, bool] = field(default_factory=dict)
    default_requested: bool = False
    default_pending: bool = False
    default_force: bool = False

    def merge(self, security_ids: list[int] | None, force: bool) -> None:
        if security_ids is None:
            if not self.default_requested or (force and not self.default_force):
                self.default_pending = True
            self.default_requested = True
            self.default_force = self.default_force or force
        else:
            for security_id in security_ids:
                if security_id not in self.requested_ids or (
                    force and not self.requested_ids[security_id]
                ):
                    self.pending_ids[security_id] = force
                self.requested_ids[security_id] = (
                    self.requested_ids.get(security_id, False) or force
                )


class PipelineCoordinator:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        source_factory: SourceFactory | None = None,
        analyzer: EventAnalyzer | None = None,
        evaluator: OutcomeEvaluator | None = None,
        x_ingestion: XIngestionService | None = None,
    ):
        self.ingestion = (
            IngestionService(session_factory, settings, source_factory=source_factory)
            if source_factory
            else IngestionService(session_factory, settings)
        )
        self.session_factory = session_factory
        self.settings = settings
        self.analyzer = analyzer or EventAnalyzer(settings)
        self.evaluator = evaluator or OutcomeEvaluator(settings=settings)
        self.x_ingestion = x_ingestion or XIngestionService(
            session_factory, settings, self.ingestion
        )
        self.telegram = (
            TelegramDigestService(session_factory, settings)
            if settings.telegram_configured
            else None
        )
        self.telegram_commands = (
            TelegramCommandService(settings, self.telegram)
            if self.telegram is not None
            else None
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="news-pipeline")
        self.x_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x-collection")
        self.daily_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily-bars")
        self._lock = threading.Lock()
        self._pipeline_future: Future[None] | None = None
        self._x_future: Future[set[int]] | None = None
        self._x_status = "idle"
        self._research_future: Future[dict[str, Any]] | None = None
        self._analysis_retry_future: Future[int] | None = None
        self._analysis_future: Future[None] | None = None
        self._evaluation_future: Future[int] | None = None
        self._cached_research_future: Future[None] | None = None
        self._daily_bars_future: Future[dict[str, Any]] | None = None
        self._daily_bars_job: _DailyBarJob | None = None
        self._shutting_down = False

    def recover_interrupted(self) -> None:
        """Close abandoned runs once, before this single-worker app starts jobs."""
        from ..daily_bar_models import DailyBarSyncState

        with self.session_factory() as session:
            for run in session.scalars(
                select(IngestionRun).where(IngestionRun.status.in_(["queued", "running"]))
            ):
                run.status = "failed"
                run.completed_at = utc_now()
                run.errors = [*(run.errors or []), "服务重启中断了任务，请重新运行"]
                run.summary = {**(run.summary or {}), "interrupted_phase": run.phase}
            for state in session.scalars(
                select(DailyBarSyncState).where(DailyBarSyncState.status == "running")
            ):
                state.status = "failed"
                state.error = "应用重启，等待重新同步"
                state.error_kind = "interrupted"
                state.next_retry_at = None
            session.commit()

    def _phase(self, run_id: int, phase: str) -> None:
        with self.session_factory() as session:
            run = session.get(IngestionRun, run_id)
            if run is not None:
                run.status = "running"
                run.phase = phase
                run.completed_at = None
                session.commit()

    def _finish_run(
        self, run_id: int, *, status: str = "complete",
        summary: dict[str, Any] | None = None, errors: list[str] | None = None,
    ) -> None:
        with self.session_factory() as session:
            run = session.get(IngestionRun, run_id)
            if run is not None:
                run.errors = list(dict.fromkeys([*(run.errors or []), *(errors or [])]))
                run.summary = {**(run.summary or {}), **(summary or {})}
                run.status = "partial" if status in {"complete", "completed"} and (
                    run.errors or run.summary.get("failed", 0)
                ) else status
                run.phase = "finished" if run.status != "failed" else run.phase
                run.completed_at = utc_now()
                session.commit()

    def submit_daily_bars(
        self, security_ids: list[int] | None = None, *, force: bool = False,
    ) -> int:
        if security_ids is not None and (
            not security_ids or any(security_id <= 0 for security_id in security_ids)
        ):
            raise ValueError("security_ids 必须是非空正整数列表")
        with self._lock:
            if self._shutting_down:
                raise PipelineBusyError("服务正在关闭")
            if self._daily_bars_job is not None:
                self._daily_bars_job.merge(security_ids, force)
                return self._daily_bars_job.run_id
            job = _DailyBarJob(self.ingestion.create_run("daily_bars"))
            job.merge(security_ids, force)
            self._daily_bars_job = job
            self._daily_bars_future = self.daily_executor.submit(self._execute_daily_bars, job)
            return job.run_id

    def _execute_daily_bars(self, job: _DailyBarJob) -> dict[str, Any]:
        result: dict[str, Any] = {"updated": 0, "skipped": 0, "failed": 0, "errors": []}
        try:
            from .daily_bars import DailyBarService

            with self.session_factory() as session:
                run = session.get(IngestionRun, job.run_id)
                if run is not None:
                    run.status = "running"
                    run.phase = "market"
                    session.commit()
            service = DailyBarService(self.settings)
            while True:
                with self._lock:
                    requests: list[tuple[list[int] | None, bool]] = []
                    if job.default_pending:
                        requests.append((None, job.default_force))
                        job.default_pending = False
                    for force in (False, True):
                        ids = sorted(
                            key for key, value in job.pending_ids.items() if value == force
                        )
                        if ids:
                            requests.append((ids, force))
                    job.pending_ids.clear()
                    if not requests:
                        self._finish_daily_bars(job, result)
                        return result
                # An explicit on-demand stock must survive a simultaneous default batch.
                for security_ids, force in requests:
                    try:
                        report = service.refresh_isolated(
                            self.session_factory, security_ids, force=force,
                        )
                        for key in ("updated", "skipped", "failed"):
                            result[key] += report.get(key, 0)
                        result["errors"].extend(report.get("errors", []))
                    except Exception as exc:
                        result["failed"] += len(security_ids) if security_ids else 1
                        result["errors"].append(f"日线同步失败（{type(exc).__name__}），可重试")
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append(f"日线任务失败（{type(exc).__name__}），可重试")
            with self._lock:
                self._finish_daily_bars(job, result)
            return result

    def _finish_daily_bars(self, job: _DailyBarJob, result: dict[str, Any]) -> None:
        """Finalize under the submission lock so no accepted IDs can be lost."""
        try:
            with self.session_factory() as session:
                run = session.get(IngestionRun, job.run_id)
                if run is not None:
                    run.status = (
                        "partial" if result["failed"] and result["updated"]
                        else "failed" if result["failed"] else "completed"
                    )
                    run.phase = "finished"
                    run.summary = {key: result[key] for key in ("updated", "skipped", "failed")}
                    run.completed_at = utc_now()
                    run.errors = result["errors"]
                    session.commit()
            if result["updated"]:
                ids = set(job.requested_ids)
                if job.default_requested:
                    with self.session_factory() as session:
                        ids.update(session.scalars(select(Watchlist.security_id).where(
                            Watchlist.active.is_(True),
                        )))
                context_run = self.ingestion.create_run("market_context")
                self._cached_research_future = self.executor.submit(
                    self._execute_cached_research, context_run, sorted(ids),
                )
        finally:
            if self._daily_bars_job is job:
                self._daily_bars_job = None

    def _execute_cached_research(self, run_id: int, security_ids: list[int]) -> None:
        from .market_research import MarketResearchService
        from .research_cycle import capture_research_state

        try:
            self._phase(run_id, "market")
            report = MarketResearchService(self.settings).refresh_cached(
                self.session_factory, security_ids,
            )
            updated = report["updated_security_ids"]
            if updated:
                self._phase(run_id, "snapshots")
                with self.session_factory() as session:
                    capture_research_state(session, self.settings, updated)
                    session.commit()
            self._finish_run(run_id, summary={"updated": len(updated),
                                              "skipped": report["skipped"]})
        except Exception as exc:
            self._finish_run(run_id, status="failed", errors=[
                f"缓存研究重算失败：{type(exc).__name__}，保留已有研究快照"
            ])

    def submit_research(self, security_ids: list[int] | None = None) -> int:
        with self._lock:
            if self.busy:
                raise PipelineBusyError("已有采集任务正在运行，请稍后刷新研究资料")
            run_id = self.ingestion.create_run("research")
            self._research_future = self.executor.submit(
                self._execute_research, run_id, security_ids
            )
            return run_id

    def _execute_research(
        self, run_id: int, security_ids: list[int] | None
    ) -> dict[str, Any]:
        from .action_evaluation import evaluate_action_snapshots
        from .analysis_context import build_analysis_context, material_research_hash
        from .market_research import MarketResearchService
        from .research_cycle import capture_research_state
        from .research_data import ResearchDataService

        previous_hashes: dict[int, str] = {}
        try:
            self._phase(run_id, "facts")
            with self.session_factory() as session:
                query = select(Security.id).where(Security.active.is_(True))
                if security_ids is None:
                    query = query.join(Watchlist).where(Watchlist.active.is_(True))
                else:
                    query = query.where(Security.id.in_(security_ids))
                previous_hashes = {
                    identity: material_research_hash(build_analysis_context(session, identity))
                    for identity in session.scalars(query)
                }
            facts = ResearchDataService(self.settings).refresh_isolated(
                self.session_factory, security_ids
            )
            invalidated = self._reconcile_research_changes(security_ids, previous_hashes)
            self._phase(run_id, "market")
            market = MarketResearchService(self.settings).refresh_isolated(
                self.session_factory, security_ids
            )
            self._phase(run_id, "snapshots")
            with self.session_factory() as session:
                from .analysis_versions import reconcile_research_inputs

                changed = reconcile_research_inputs(
                    session, security_ids, previous_hashes=previous_hashes,
                )
                invalidated = sorted(set(invalidated) | set(changed))
                if changed:
                    rebuild_signal_snapshots(session)
                captured = capture_research_state(session, self.settings, security_ids)
                session.commit()
            self._phase(run_id, "evaluation")
            with self.session_factory() as session:
                outcomes = evaluate_action_snapshots(session, self.settings)
                run = session.get(IngestionRun, run_id)
                if run:
                    run.analyses_created = captured["snapshots"]
                session.commit()
            source_failures = [item for item in facts.get("sources", [])
                               if item.get("status") not in {"available", "disabled"}]
            market_failures = [item for item in market.get("results", [])
                               if item.get("status") != "ready"]
            # Availability gaps are terminal partial results, never a successful empty refresh.
            failures = len(source_failures) + len(market_failures)
            self._finish_run(run_id, status="completed", summary={
                "failed": failures, "snapshots": captured["snapshots"],
                "evaluation_completed": outcomes.get("completed", 0),
                "evaluation_pending": outcomes.get("pending", 0),
                "reanalysis_required": len(invalidated),
            }, errors=[f"{item.get('source', '研究来源')}：{item.get('status', 'unavailable')}"
                       for item in source_failures] + [
                f"证券 #{item.get('security_id', '?')}：行情资料未齐备"
                for item in market_failures
            ])
            return {"facts": facts, "market": market, "captured": captured, "outcomes": outcomes}
        except Exception as exc:
            # A source can commit useful facts before a later source or market fails.
            try:
                self._reconcile_research_changes(security_ids, previous_hashes)
            except Exception:
                logger.exception("Could not reconcile committed research inputs for run %s", run_id)
            self._finish_run(run_id, status="failed", errors=[
                f"研究刷新失败：{type(exc).__name__}，已保留成功记录"
            ])
            raise

    def _reconcile_research_changes(
        self, security_ids: list[int] | None, previous_hashes: dict[int, str],
    ) -> list[int]:
        from .analysis_versions import reconcile_research_inputs

        with self.session_factory() as session:
            changed = reconcile_research_inputs(
                session, security_ids, previous_hashes=previous_hashes,
            )
            if changed:
                rebuild_signal_snapshots(session)
            session.commit()
            return changed

    def submit_pipeline(self, trigger: str) -> int:
        with self._lock:
            if self.busy:
                raise PipelineBusyError("已有采集任务正在运行")
            run_id = self.ingestion.create_run(trigger)
            self._pipeline_future = self.executor.submit(self._execute_pipeline, run_id)
            return run_id

    def _execute_pipeline(self, run_id: int) -> None:
        try:
            self._phase(run_id, "ingestion")
            self.ingestion.execute_run(run_id, finalize=False)
            analyzed = 0
            processed_ids: list[int] = []
            self._phase(run_id, "analysis")
            with self.session_factory() as session:
                if self.settings.auto_analyze:
                    analyzed = self.analyzer.analyze_pending(session)
                    processed_ids = getattr(self.analyzer, "last_processed_event_ids", [])
                rebuild_signal_snapshots(session)
                session.commit()
            self._phase(run_id, "snapshots")
            with self.session_factory() as session:
                if self.settings.research_refresh_enabled:
                    from .research_cycle import capture_research_state

                    capture_research_state(session, self.settings)
                    session.commit()
            self._phase(run_id, "evaluation")
            with self.session_factory() as session:
                evaluated = self.evaluator.evaluate(session)
                run = session.get(IngestionRun, run_id)
                if run is not None:
                    run.analyses_created = session.scalar(
                        select(func.count()).select_from(EventSecurityImpact).where(
                            EventSecurityImpact.event_id.in_(processed_ids),
                            EventSecurityImpact.created_at >= run.started_at,
                            EventSecurityImpact.status == "complete",
                            EventSecurityImpact.is_current.is_(True),
                        )
                    ) or 0
                failures = session.scalar(select(func.count()).select_from(Event).where(
                    Event.id.in_(processed_ids),
                    Event.status.in_(["partial", "error", "unavailable"]),
                ))
                session.commit()
            self._finish_run(run_id, summary={
                "analyzed": analyzed, "evaluated": evaluated, "failed": failures or 0,
            }, errors=[f"{failures} 个事件未完整分析，请查看错误或重试状态"] if failures else [])
        except Exception as exc:
            logger.exception("Pipeline run %s failed", run_id)
            try:
                with self.session_factory() as session:
                    run = session.get(IngestionRun, run_id)
                    if run is not None:
                        message = f"pipeline: {type(exc).__name__}，任务失败，已保留成功记录"
                        run.errors = [*(run.errors or []), message]
                        run.status = "failed"
                        run.completed_at = utc_now()
                        session.commit()
            except Exception:
                logger.exception("Could not persist failure for pipeline run %s", run_id)
            raise

    def wait_for_pipeline(self) -> None:
        """Wait for the active pipeline and propagate its exception to the caller."""
        future = self._pipeline_future
        if future is not None:
            future.result()

    def submit_x_ingestion(self) -> Future[set[int]]:
        with self._lock:
            if self._shutting_down or (self._x_future is not None and not self._x_future.done()):
                raise PipelineBusyError("X 帖子正在抓取或筛选，请等待当前任务完成")
            self._x_status = "collecting"
            self._x_future = self.x_executor.submit(self._execute_x_ingestion)
            return self._x_future

    def _execute_x_ingestion(self) -> set[int]:
        try:
            self.x_ingestion.execute(screen_posts=False)
            self._x_status = "queued_screening"
            # Keep model work on the existing worker; raw posts are already committed.
            result = self.executor.submit(self._finish_x_ingestion).result()
            self._x_status = "completed"
            return result
        except Exception:
            self._x_status = "failed"
            raise

    def _finish_x_ingestion(self) -> set[int]:
        self._x_status = "screening"
        queued_events = self.x_ingestion.screen_pending()
        with self.session_factory() as session:
            from .research_cycle import sync_claims_and_reconcile

            sync_claims_and_reconcile(session)
            session.commit()
            if self.settings.auto_analyze and queued_events:
                for event_id in sorted(queued_events):
                    event = session.get(Event, event_id)
                    if event is not None and event.status in {"pending", "unavailable"}:
                        self.analyzer.analyze_event(session, event)
            rebuild_signal_snapshots(session)
            if self.settings.research_refresh_enabled:
                from .research_cycle import capture_research_state

                capture_research_state(session, self.settings)
            session.commit()
        return queued_events

    @property
    def x_status(self) -> str:
        return self._x_status

    def submit_analysis(self, event_id: int) -> Future[None]:
        with self._lock:
            if self.busy:
                raise PipelineBusyError("已有任务正在运行，请稍后重新分析")
            self._analysis_future = self.executor.submit(self._execute_analysis, event_id)
            return self._analysis_future

    def submit_analysis_retry(self) -> Future[int] | None:
        with self._lock:
            if self.busy:
                raise PipelineBusyError("已有任务正在运行，稍后重试分析")
            with self.session_factory() as session:
                due = session.scalar(select(Event.id).where(
                    Event.status.in_(["partial", "error"]),
                    Event.analysis_attempts < MAX_ANALYSIS_ATTEMPTS,
                    Event.analysis_next_retry_at <= utc_now(),
                ).limit(1))
            if due is None:
                return None
            self._analysis_retry_future = self.executor.submit(self._execute_analysis_retries)
            return self._analysis_retry_future

    def _execute_analysis_retries(self) -> int:
        with self.session_factory() as session:
            count = self.analyzer.analyze_pending(session)
            if count:
                rebuild_signal_snapshots(session)
                if self.settings.research_refresh_enabled:
                    from .research_cycle import capture_research_state

                    capture_research_state(session, self.settings)
                    session.commit()
            return count

    def _execute_analysis(self, event_id: int) -> None:
        with self.session_factory() as session:
            event = session.get(Event, event_id)
            if event:
                self.analyzer.analyze_event(session, event)
                rebuild_signal_snapshots(session)

    def submit_evaluation(self) -> Future[int]:
        with self._lock:
            if self.busy:
                raise PipelineBusyError("已有任务正在运行，请稍后验证")
            self._evaluation_future = self.executor.submit(self._execute_evaluation)
            return self._evaluation_future

    def _execute_evaluation(self) -> int:
        with self.session_factory() as session:
            count = self.evaluator.evaluate(session)
            from ..risk_models import ActionValidationState

            manual_pending = session.scalar(select(ActionValidationState.snapshot_id).where(
                ActionValidationState.status == "queued",
            ).limit(1)) is not None
            if self.settings.research_refresh_enabled or manual_pending:
                from .action_evaluation import evaluate_action_snapshots

                evaluate_action_snapshots(session, self.settings, provider=self.evaluator.provider)
                session.commit()
            return count

    def submit_telegram_digest(self) -> Future[None]:
        if self.telegram is None:
            raise RuntimeError("Telegram 未配置")
        with self._lock:
            if self._shutting_down:
                raise PipelineBusyError("服务正在关闭")
            return self.executor.submit(self._execute_telegram_digest)

    def _execute_telegram_digest(self) -> None:
        assert self.telegram is not None
        try:
            self.telegram.send_digest()
        except Exception as exc:
            logger.error(
                "Telegram digest delivery failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def poll_telegram_commands(self) -> int:
        if self.telegram_commands is None:
            raise RuntimeError("Telegram 未配置")
        try:
            return self.telegram_commands.poll()
        except Exception as exc:
            logger.error(
                "Telegram command polling failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return 0

    @property
    def busy(self) -> bool:
        pipeline_busy = bool(self._pipeline_future and not self._pipeline_future.done())
        x_busy = bool(self._x_future and not self._x_future.done())
        research_busy = bool(self._research_future and not self._research_future.done())
        retry_busy = bool(self._analysis_retry_future and not self._analysis_retry_future.done())
        analysis_busy = bool(self._analysis_future and not self._analysis_future.done())
        evaluation_busy = bool(self._evaluation_future and not self._evaluation_future.done())
        cached_busy = bool(self._cached_research_future and not self._cached_research_future.done())
        return (
            self._shutting_down or pipeline_busy or x_busy or research_busy or retry_busy
            or analysis_busy or evaluation_busy or cached_busy
        )

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        # A collecting task may still enqueue its screening work during shutdown.
        # Never hold the coordination lock while waiting for workers to finish.
        self.x_executor.shutdown(wait=True, cancel_futures=False)
        self.daily_executor.shutdown(wait=True, cancel_futures=False)
        self.executor.shutdown(wait=True, cancel_futures=False)
