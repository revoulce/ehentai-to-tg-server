import time
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from ehtg.analysis import AnalysisLease, AnalysisQueue
from ehtg.domain.errors import AppError
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.source.locking import source_lock


class SourceInspector(Protocol):
    def inspect(self, source_url: str, heartbeat: Callable[[], None]) -> GallerySnapshot: ...


class AnalysisWorker:
    def __init__(self, queue: AnalysisQueue, source: SourceInspector) -> None:
        self.queue = queue
        self.source = source
        self.worker_id = str(uuid4())

    def run_once(self) -> bool:
        with source_lock(self.queue.db.path) as acquired:
            if not acquired:
                return False
            lease = self.queue.claim(self.worker_id)
            if lease is None:
                return False

            def heartbeat() -> None:
                self.queue.heartbeat(lease)

            try:
                snapshot = self.source.inspect(lease.source_url, heartbeat)
            except AppError as error:
                if error.code != "ANALYSIS_LEASE_LOST":
                    self._fail(lease, error)
                return True
            except Exception:
                # Neither raw source output nor exception text belongs in the queue/history.
                self._fail(
                    lease, AppError("EXTRACTOR_INCOMPATIBLE", "Не удалось разобрать источник.")
                )
                return True
            try:
                self.queue.complete(lease, snapshot)
            except AppError as error:
                if error.code != "ANALYSIS_LEASE_LOST":
                    self._fail(lease, error)
            return True

    def _fail(self, lease: AnalysisLease, error: AppError) -> None:
        try:
            self.queue.fail(lease, error)
        except AppError as failure:
            if failure.code != "ANALYSIS_LEASE_LOST":
                raise

    def run(self, *, poll_seconds: float = 1) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)
