import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot, canonical_json
from ehtg.domain.source import parse_gallery_url
from ehtg.storage.database import Database
from ehtg.storage.snapshots import save_analysis


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class GalleryService:
    """Shared use cases. API and future bot call the same transactional operations."""

    def __init__(
        self,
        database: Database,
        owner_id: str,
        channel_id: str,
        *,
        max_pages: int = 2000,
        max_queue: int = 100,
    ) -> None:
        self.db = database
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.max_pages = max_pages
        self.max_queue = max_queue

    def _draft(self, connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM drafts WHERE id=? AND owner_id=?", (draft_id, self.owner_id)
        ).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Черновик не найден.", 404)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _revision(row: sqlite3.Row, expected: int) -> None:
        if row["revision"] != expected:
            raise AppError("STALE_REVISION", "Черновик изменился. Обновите предпросмотр.", 409)

    def create_draft(self, source_url: str, selection: PageSelection) -> dict[str, Any]:
        source = parse_gallery_url(source_url)
        draft_id = str(uuid4())
        with self.db.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE owner_id=? AND state IN ('analyzing','ready')",
                (self.owner_id,),
            ).fetchone()[0]
            if count >= self.max_queue:
                raise AppError(
                    "RESOURCE_LIMIT", "Достигнут предел неподтверждённых черновиков.", 409
                )
            connection.execute(
                "INSERT INTO drafts(id,owner_id,gid,source_url,state,selection_json,created_at) "
                "VALUES (?,?,?,?,'analyzing',?,?)",
                (
                    draft_id,
                    self.owner_id,
                    source.gid,
                    source.canonical,
                    canonical_json(asdict(selection)),
                    utc_now(),
                ),
            )
            connection.execute("INSERT INTO draft_analysis(draft_id) VALUES (?)", (draft_id,))
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            row = self._draft(connection, draft_id)
            result = dict(row)
            result["page_selection"] = json.loads(result.pop("selection_json"))
            result["snapshot"] = None
            result["selected_pages"] = []
            analysis = connection.execute(
                "SELECT state,attempts,retry_at,error_code,error_message,retryable "
                "FROM draft_analysis WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            result["analysis"] = (
                dict(analysis)
                if analysis is not None
                else {
                    "state": "succeeded",
                    "attempts": 0,
                    "retry_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                }
            )
            if row["snapshot_id"]:
                snapshot = connection.execute(
                    "SELECT * FROM gallery_snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()
                metadata = json.loads(snapshot["metadata_json"])
                result["snapshot"] = {**metadata, "fingerprint": snapshot["fingerprint"]}
                result["selected_pages"] = list(
                    PageSelection(**result["page_selection"]).resolve(
                        len(metadata["page_ids"]), max_pages=self.max_pages
                    )
                )
            return result

    def record_analysis(
        self,
        draft_id: str,
        expected_revision: int,
        snapshot: GallerySnapshot,
    ) -> None:
        """Internal adapter boundary, deliberately not available as an HTTP operation."""
        with self.db.transaction() as connection:
            row = self._draft(connection, draft_id)
            self._revision(row, expected_revision)
            if row["state"] != "analyzing":
                raise AppError("DRAFT_NOT_ANALYZING", "Анализ уже завершён.", 409)
            analysis = connection.execute(
                "SELECT state,attempts FROM draft_analysis WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if analysis is None or analysis["state"] != "pending" or analysis["attempts"] != 0:
                raise AppError("ANALYSIS_LEASE_LOST", "Результат анализа устарел.", 409)
            save_analysis(connection, row, snapshot, max_pages=self.max_pages, created_at=utc_now())
            connection.execute(
                "UPDATE draft_analysis SET state='succeeded' WHERE draft_id=?", (draft_id,)
            )

    def edit_selection(
        self,
        draft_id: str,
        expected_revision: int,
        selection: PageSelection,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            row = self._draft(connection, draft_id)
            self._revision(row, expected_revision)
            if row["state"] != "ready":
                raise AppError("DRAFT_NOT_READY", "Редактирование доступно после анализа.", 409)
            metadata = json.loads(
                connection.execute(
                    "SELECT metadata_json FROM gallery_snapshots WHERE id=?",
                    (row["snapshot_id"],),
                ).fetchone()[0]
            )
            selection.resolve(len(metadata["page_ids"]), max_pages=self.max_pages)
            connection.execute(
                "UPDATE drafts SET selection_json=?, revision=revision+1 WHERE id=?",
                (canonical_json(asdict(selection)), draft_id),
            )
        return self.get_draft(draft_id)

    def publish(
        self,
        draft_id: str,
        expected_revision: int,
        key: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
            raise AppError("INVALID_IDEMPOTENCY_KEY", "Нужен ключ подтверждения до 128 символов.")
        operation = f"publish:{draft_id}"
        request_hash = hashlib.sha256(canonical_json({"revision": expected_revision}).encode())
        digest = request_hash.hexdigest()
        with self.db.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM idempotency WHERE owner_id=? AND operation=? AND key=?",
                (self.owner_id, operation, key),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != digest:
                    raise AppError(
                        "IDEMPOTENCY_CONFLICT", "Ключ уже использован с другим запросом.", 409
                    )
                return dict(json.loads(previous["result_json"]))
            draft = self._draft(connection, draft_id)
            self._revision(draft, expected_revision)
            if draft["state"] != "ready":
                raise AppError("DRAFT_NOT_READY", "Сначала дождитесь готового предпросмотра.", 409)
            existing = connection.execute(
                "SELECT id, state, published_pages FROM jobs WHERE owner_id=? AND gid=? "
                "AND channel_id=? AND (state NOT IN ('failed','cancelled') OR published_pages>0)",
                (self.owner_id, draft["gid"], self.channel_id),
            ).fetchone()
            if existing is not None:
                code = (
                    "ALREADY_PUBLISHED"
                    if existing["state"] == "completed" or existing["published_pages"]
                    else "ALREADY_QUEUED"
                )
                raise AppError(
                    code,
                    "Галерея уже принята или опубликована.",
                    409,
                    details={"job_id": existing["id"]},
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('completed','failed','cancelled')"
            ).fetchone()[0]
            if count >= self.max_queue:
                raise AppError("RESOURCE_LIMIT", "Очередь заполнена.", 409)
            snapshot = json.loads(
                connection.execute(
                    "SELECT metadata_json FROM gallery_snapshots WHERE id=?",
                    (draft["snapshot_id"],),
                ).fetchone()[0]
            )
            pages = PageSelection(**json.loads(draft["selection_json"])).resolve(
                len(snapshot["page_ids"]), max_pages=self.max_pages
            )
            job_id = str(uuid4())
            created_at = utc_now()
            connection.execute(
                "INSERT INTO jobs(id,draft_id,owner_id,gid,channel_id,snapshot_id,"
                "selection_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    draft_id,
                    self.owner_id,
                    draft["gid"],
                    self.channel_id,
                    draft["snapshot_id"],
                    draft["selection_json"],
                    created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO job_pages(job_id,source_page_index,page_identity) VALUES (?,?,?)",
                [(job_id, page, snapshot["page_ids"][page - 1]) for page in pages],
            )
            connection.execute("UPDATE drafts SET state='confirmed' WHERE id=?", (draft_id,))
            result = {"job_id": job_id, "state": "queued", "created_at": created_at}
            connection.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?,?,?)",
                (self.owner_id, operation, key, digest, canonical_json(result), created_at),
            )
            connection.execute(
                "INSERT INTO events(object_id,kind,actor,request_id,created_at) VALUES (?,?,?,?,?)",
                (job_id, "job_queued", self.owner_id, request_id, created_at),
            )
            return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, self.owner_id)
            ).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "Задание не найдено.", 404)
            result = dict(row)
            result["page_selection"] = json.loads(result.pop("selection_json"))
            result["pages"] = [
                dict(page)
                for page in connection.execute(
                    "SELECT source_page_index,page_identity,state FROM job_pages "
                    "WHERE job_id=? ORDER BY source_page_index",
                    (job_id,),
                )
            ]
            return result

    def list_jobs(self, *, after: int = 0, limit: int = 50) -> dict[str, Any]:
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT sequence,id,state,created_at,published_pages,published_parts "
                "FROM jobs WHERE owner_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (self.owner_id, after, limit + 1),
            ).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {
                "items": items,
                "next_cursor": items[-1]["sequence"] if len(rows) > limit else None,
            }
