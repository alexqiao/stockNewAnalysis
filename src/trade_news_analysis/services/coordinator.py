"""Single-worker background coordination for ingestion, analysis, and evaluation."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from ..config import Settings
from ..db import SessionFactory
from ..models import Event
from .analysis import EventAnalyzer
from .evaluation import OutcomeEvaluator
from .ingestion import IngestionService, SourceFactory
from .scoring import rebuild_signal_snapshots


class PipelineBusyError(RuntimeError):
    pass


class PipelineCoordinator:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        source_factory: SourceFactory | None = None,
        analyzer: EventAnalyzer | None = None,
        evaluator: OutcomeEvaluator | None = None,
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
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="news-pipeline")
        self._lock = threading.Lock()
        self._pipeline_future: Future[None] | None = None

    def submit_pipeline(self, trigger: str) -> int:
        with self._lock:
            if self._pipeline_future and not self._pipeline_future.done():
                raise PipelineBusyError("已有采集任务正在运行")
            run_id = self.ingestion.create_run(trigger)
            self._pipeline_future = self.executor.submit(self._execute_pipeline, run_id)
            return run_id

    def _execute_pipeline(self, run_id: int) -> None:
        self.ingestion.execute_run(run_id)
        with self.session_factory() as session:
            if self.settings.auto_analyze:
                self.analyzer.analyze_pending(session)
                rebuild_signal_snapshots(session)
            self.evaluator.evaluate(session)

    def submit_analysis(self, event_id: int) -> Future[None]:
        return self.executor.submit(self._execute_analysis, event_id)

    def _execute_analysis(self, event_id: int) -> None:
        with self.session_factory() as session:
            event = session.get(Event, event_id)
            if event:
                self.analyzer.analyze_event(session, event)
                rebuild_signal_snapshots(session)

    def submit_evaluation(self) -> Future[int]:
        return self.executor.submit(self._execute_evaluation)

    def _execute_evaluation(self) -> int:
        with self.session_factory() as session:
            return self.evaluator.evaluate(session)

    @property
    def busy(self) -> bool:
        return bool(self._pipeline_future and not self._pipeline_future.done())

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
