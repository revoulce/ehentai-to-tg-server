import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from ehtg.domain.errors import AppError
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.service import utc_now
from ehtg.storage.database import Database
from ehtg.storage.snapshots import save_analysis

SOURCE_PAUSES = frozenset(
    {
        "SOURCE_AUTH_REQUIRED",
        "SOURCE_QUOTA_EXCEEDED",
        "SOURCE_TEMPORARILY_BLOCKED",
    }
)


@dataclass(frozen=True)
class AnalysisLease:
    draft_id: str
    source_url: str
    revision: int
    worker_id: str
    generation: int
    attempt: int


class AnalysisQueue:
    def __init__(
        self,
        database: Database,
        owner_id: str,
        *,
        lease_seconds: float = 30,
        max_attempts: int = 3,
        retry_seconds: float = 5,
        max_pages: int = 2000,
        max_queue: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = database
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds
        self.max_pages = max_pages
        self.max_queue = max_queue
        self.clock = clock

    def claim(self, worker_id: str) -> AnalysisLease | None:
        now = self.clock()
        with self.db.transaction() as connection:
            if connection.execute("SELECT paused FROM source_control").fetchone()[0]:
                return None
            # A crash also consumes an attempt; repeated crashes cannot cause an infinite loop.
            expired = connection.execute(
                "SELECT a.draft_id FROM draft_analysis a JOIN drafts d ON d.id=a.draft_id "
                "WHERE d.owner_id=? AND d.state='analyzing' AND a.state='running' "
                "AND a.lease_until<=? AND a.attempts>=?",
                (self.owner_id, now, self.max_attempts),
            ).fetchall()
            for item in expired:
                connection.execute(
                    "UPDATE draft_analysis SET state='failed',lease_owner=NULL,lease_until=NULL,"
                    "error_code='ANALYSIS_RETRIES_EXHAUSTED',"
                    "error_message='Исчерпаны попытки анализа.',retryable=0 WHERE draft_id=?",
                    (item[0],),
                )
                connection.execute(
                    "UPDATE drafts SET state='error',revision=revision+1 WHERE id=?", (item[0],)
                )
                self._event(connection, item[0], "analysis_exhausted", "worker")
            if (
                connection.execute(
                    "SELECT 1 FROM draft_analysis WHERE state='running' AND lease_until>? LIMIT 1",
                    (now,),
                ).fetchone()
                is not None
            ):
                return None
            row = connection.execute(
                "SELECT a.*,d.source_url,d.revision FROM draft_analysis a "
                "JOIN drafts d ON d.id=a.draft_id WHERE d.owner_id=? AND d.state='analyzing' AND "
                "(a.state='pending' OR (a.state='retry_wait' AND a.retry_at<=?) OR "
                "(a.state='running' AND a.lease_until<=?)) ORDER BY a.sequence LIMIT 1",
                (self.owner_id, now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE draft_analysis SET state='running',attempts=attempts+1,"
                "generation=generation+1,"
                "lease_owner=?,lease_until=?,retry_at=NULL WHERE draft_id=?",
                (worker_id, now + self.lease_seconds, row["draft_id"]),
            )
            self._event(connection, row["draft_id"], "analysis_claimed", worker_id)
            return AnalysisLease(
                row["draft_id"],
                row["source_url"],
                row["revision"],
                worker_id,
                row["generation"] + 1,
                row["attempts"] + 1,
            )

    def _require_lease(self, connection: sqlite3.Connection, lease: AnalysisLease) -> sqlite3.Row:
        row = connection.execute(
            "SELECT d.* FROM draft_analysis a JOIN drafts d ON d.id=a.draft_id "
            "WHERE a.draft_id=? AND d.owner_id=? AND d.state='analyzing' AND d.revision=? "
            "AND a.state='running' AND a.generation=? AND a.lease_owner=? AND a.lease_until>?",
            (
                lease.draft_id,
                self.owner_id,
                lease.revision,
                lease.generation,
                lease.worker_id,
                self.clock(),
            ),
        ).fetchone()
        if row is None:
            raise AppError("ANALYSIS_LEASE_LOST", "Попытка анализа уже не владеет черновиком.", 409)
        return cast(sqlite3.Row, row)

    def heartbeat(self, lease: AnalysisLease) -> None:
        with self.db.transaction() as connection:
            self._require_lease(connection, lease)
            connection.execute(
                "UPDATE draft_analysis SET lease_until=? WHERE draft_id=?",
                (self.clock() + self.lease_seconds, lease.draft_id),
            )

    def complete(self, lease: AnalysisLease, snapshot: GallerySnapshot) -> None:
        with self.db.transaction() as connection:
            draft = self._require_lease(connection, lease)
            save_analysis(
                connection, draft, snapshot, max_pages=self.max_pages, created_at=utc_now()
            )
            connection.execute(
                "UPDATE draft_analysis SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
                "error_code=NULL,error_message=NULL,retryable=0 WHERE draft_id=?",
                (lease.draft_id,),
            )
            self._event(connection, lease.draft_id, "analysis_ready", lease.worker_id)

    def fail(self, lease: AnalysisLease, error: AppError) -> None:
        pause = error.code in SOURCE_PAUSES
        retry = error.retryable and lease.attempt < self.max_attempts and not pause
        state = "paused" if pause else ("retry_wait" if retry else "failed")
        retry_at = (
            self.clock() + min(300, self.retry_seconds * 2 ** (lease.attempt - 1))
            if retry
            else None
        )
        with self.db.transaction() as connection:
            self._require_lease(connection, lease)
            connection.execute(
                "UPDATE draft_analysis SET state=?,lease_owner=NULL,lease_until=NULL,retry_at=?,"
                "error_code=?,error_message=?,retryable=? WHERE draft_id=?",
                (state, retry_at, error.code, error.message, int(retry), lease.draft_id),
            )
            if pause:
                connection.execute(
                    "UPDATE source_control SET paused=1,reason_code=?", (error.code,)
                )
            if state == "failed":
                connection.execute(
                    "UPDATE drafts SET state='error',revision=revision+1 WHERE id=?",
                    (lease.draft_id,),
                )
            self._event(connection, lease.draft_id, f"analysis_{state}", lease.worker_id)

    def status(self, draft_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT a.state,a.attempts,a.retry_at,a.error_code,a.error_message,a.retryable "
                "FROM draft_analysis a JOIN drafts d ON d.id=a.draft_id "
                "WHERE a.draft_id=? AND d.owner_id=?",
                (draft_id, self.owner_id),
            ).fetchone()
            return (
                dict(row)
                if row is not None
                else {
                    "state": "succeeded",
                    "attempts": 0,
                    "retry_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                }
            )

    def _editable(
        self, connection: sqlite3.Connection, draft_id: str, revision: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM drafts WHERE id=? AND owner_id=?", (draft_id, self.owner_id)
        ).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Черновик не найден.", 404)
        if row["revision"] != revision:
            raise AppError("STALE_REVISION", "Черновик изменился. Обновите предпросмотр.", 409)
        return cast(sqlite3.Row, row)

    def cancel(self, draft_id: str, revision: int) -> None:
        with self.db.transaction() as connection:
            row = self._editable(connection, draft_id, revision)
            if row["state"] != "analyzing":
                raise AppError("ANALYSIS_NOT_RUNNING", "Анализ уже завершён.", 409)
            connection.execute(
                "UPDATE draft_analysis SET state='cancelled',generation=generation+1,"
                "lease_owner=NULL,lease_until=NULL,retry_at=NULL,error_code='ANALYSIS_CANCELLED',"
                "error_message='Анализ отменён владельцем.',retryable=0 WHERE draft_id=?",
                (draft_id,),
            )
            connection.execute(
                "UPDATE drafts SET state='error',revision=revision+1 WHERE id=?", (draft_id,)
            )
            self._event(connection, draft_id, "analysis_cancelled", self.owner_id)

    def retry(self, draft_id: str, revision: int) -> None:
        with self.db.transaction() as connection:
            row = self._editable(connection, draft_id, revision)
            if row["state"] != "error":
                raise AppError(
                    "ANALYSIS_NOT_FAILED", "Повтор доступен после ошибки или отмены.", 409
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE owner_id=? AND state IN ('analyzing','ready')",
                (self.owner_id,),
            ).fetchone()[0]
            if count >= self.max_queue:
                raise AppError("RESOURCE_LIMIT", "Достигнут предел черновиков.", 409)
            connection.execute(
                "UPDATE draft_analysis SET state='pending',attempts=0,generation=generation+1,"
                "lease_owner=NULL,lease_until=NULL,retry_at=NULL,error_code=NULL,"
                "error_message=NULL,retryable=0 WHERE draft_id=?",
                (draft_id,),
            )
            connection.execute(
                "UPDATE drafts SET state='analyzing',revision=revision+1 WHERE id=?", (draft_id,)
            )
            self._event(connection, draft_id, "analysis_retried", self.owner_id)

    def resume_source(self) -> None:
        """Trusted caller verifies the installed session under the source lock first."""
        with self.db.transaction() as connection:
            connection.execute("UPDATE source_control SET paused=0,reason_code=NULL")
            connection.execute(
                "UPDATE draft_analysis SET state='pending',attempts=0,error_code=NULL,"
                "error_message=NULL,retryable=0 WHERE state='paused' AND draft_id IN "
                "(SELECT id FROM drafts WHERE owner_id=?)",
                (self.owner_id,),
            )

    @staticmethod
    def _event(connection: sqlite3.Connection, draft_id: str, kind: str, actor: str) -> None:
        connection.execute(
            "INSERT INTO events(object_id,kind,actor,created_at) VALUES (?,?,?,?)",
            (draft_id, kind, actor, utc_now()),
        )
