import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ehtg.analysis import SOURCE_PAUSES
from ehtg.artifacts import ArtifactStore
from ehtg.domain.errors import AppError
from ehtg.domain.publication import PublicationOptions, publication_plan
from ehtg.service import GalleryService, utc_now
from ehtg.settings import Settings
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.locking import source_lock
from ehtg.telegram import TelegramClient


class JobControl:
    def __init__(self, service: GalleryService) -> None:
        self.service = service
        self.db = service.db

    def pause(self, paused: bool) -> dict[str, bool]:
        with self.db.transaction() as connection:
            connection.execute("UPDATE queue_control SET paused=?", (paused,))
        return {"paused": paused}

    def action(self, job_id: str, expected: int, action: str) -> dict[str, Any]:
        self.service.get_job(job_id)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.service._revision(row, expected)
            if row["state"] in {"completed", "cancelled"}:
                raise AppError("JOB_TERMINAL", "Задание уже завершено.", 409)
            if action == "cancel":
                sending = connection.execute(
                    "SELECT 1 FROM publication_parts WHERE job_id=? AND state='sending'", (job_id,)
                ).fetchone()
                if sending and row["state"] != "needs_review":
                    connection.execute(
                        "UPDATE jobs SET cancel_requested=1,revision=revision+1 WHERE id=?",
                        (job_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET state='cancelled',cancel_requested=1,"
                        "revision=revision+1 WHERE id=?",
                        (job_id,),
                    )
            elif action == "retry":
                if row["state"] not in {"failed", "paused", "retry_wait"} or row["error_code"] in {
                    "SOURCE_VERSION_CHANGED",
                    "DELIVERY_UNCERTAIN",
                    "ANIMATION_REVIEW",
                    "PUBLICATION_RECONFIRM_REQUIRED",
                }:
                    raise AppError("REVIEW_REQUIRED", "Сначала разрешите причину остановки.", 409)
                conflict = connection.execute(
                    "SELECT 1 FROM jobs WHERE id<>? AND owner_id=? AND gid=? AND channel_id=? "
                    "AND (state NOT IN ('completed','failed','cancelled') OR "
                    "(? IS NULL AND (state='completed' OR published_pages>0))) LIMIT 1",
                    (
                        job_id,
                        self.service.owner_id,
                        row["gid"],
                        row["channel_id"],
                        row["repost_of"],
                    ),
                ).fetchone()
                if conflict:
                    raise AppError("ALREADY_QUEUED", "Другое задание галереи уже принято.", 409)
                connection.execute(
                    "UPDATE jobs SET state='queued',attempts=0,retry_at=NULL,"
                    "error_code=NULL,error_message=NULL,revision=revision+1 "
                    "WHERE id=?",
                    (job_id,),
                )
            else:
                raise AppError("INVALID_ACTION", "Недопустимое действие.")
            connection.execute(
                "INSERT INTO events(object_id,kind,actor,created_at) VALUES (?,?,?,?)",
                (job_id, action, self.service.owner_id, utc_now()),
            )
        return self.service.get_job(job_id)

    def review(
        self,
        job_id: str,
        expected: int,
        decision: str,
        *,
        evidence: str = "",
        page: int | None = None,
    ) -> dict[str, Any]:
        self.service.get_job(job_id)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.service._revision(row, expected)
            if row["state"] != "needs_review":
                raise AppError("REVIEW_NOT_REQUIRED", "Сверка для задания не требуется.", 409)
            if row["error_code"] == "DELIVERY_UNCERTAIN" and decision in {"sent", "not_sent"}:
                part = connection.execute(
                    "SELECT * FROM publication_parts WHERE job_id=? AND state='sending'", (job_id,)
                ).fetchone()
                if part is None or (decision == "sent" and not evidence.strip()):
                    raise AppError(
                        "EVIDENCE_REQUIRED", "Укажите ссылку или доказательство публикации."
                    )
                state = "sent" if decision == "sent" else "pending"
                connection.execute(
                    "UPDATE publication_parts SET state=?,evidence=? WHERE job_id=? "
                    "AND part_index=?",
                    (state, evidence, job_id, part["part_index"]),
                )
                if decision == "sent":
                    plan = json.loads(row["plan_json"])
                    count = len(plan["parts"][part["part_index"] - 1]["pages"])
                    connection.execute(
                        "UPDATE jobs SET published_pages=published_pages+?, "
                        "published_parts=published_parts+1 WHERE id=?",
                        (count, job_id),
                    )
            elif row["error_code"] == "ANIMATION_REVIEW" and decision in {"first_frame", "exclude"}:
                review = json.loads(row["review_json"])
                if page != review.get("page"):
                    raise AppError("INVALID_REVIEW", "Номер страницы не совпадает с остановкой.")
                if decision == "exclude":
                    plan = json.loads(row["plan_json"])
                    pages = [p for part in plan["parts"] for p in part["pages"] if p != page]
                    metadata = json.loads(
                        connection.execute(
                            "SELECT metadata_json FROM gallery_snapshots WHERE id=?",
                            (row["snapshot_id"],),
                        ).fetchone()[0]
                    )
                    source_url = connection.execute(
                        "SELECT source_url FROM drafts WHERE id=?", (row["draft_id"],)
                    ).fetchone()[0]
                    plan = publication_plan(
                        metadata, source_url, pages, PublicationOptions(**plan["options"])
                    )
                    connection.execute(
                        "DELETE FROM job_pages WHERE job_id=? AND source_page_index=?",
                        (job_id, page),
                    )
                    connection.execute(
                        "UPDATE jobs SET plan_json=?,selection_json=? WHERE id=?",
                        (
                            json.dumps(plan),
                            json.dumps({"include": ",".join(map(str, pages)), "exclude": ""}),
                            job_id,
                        ),
                    )
                else:
                    review.setdefault("first_frames", []).append(page)
                connection.execute(
                    "UPDATE jobs SET review_json=? WHERE id=?", (json.dumps(review), job_id)
                )
            elif decision == "cancel":
                connection.execute(
                    "UPDATE jobs SET state='cancelled',cancel_requested=1,"
                    "revision=revision+1 WHERE id=?",
                    (job_id,),
                )
            else:
                raise AppError("INVALID_REVIEW", "Решение не соответствует причине остановки.")
            connection.execute(
                "UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' "
                "ELSE 'queued' END,error_code=NULL,error_message=NULL,retry_at=NULL,"
                "revision=revision+1 WHERE id=?",
                (job_id,),
            )
            connection.execute(
                "INSERT INTO events(object_id,kind,actor,created_at) VALUES (?,?,?,?)",
                (job_id, "review_" + decision, self.service.owner_id, utc_now()),
            )
        return self.service.get_job(job_id)


class PublicationWorker:
    def __init__(
        self,
        service: GalleryService,
        settings: Settings,
        source: GalleryDLSource,
        telegram: TelegramClient,
        *,
        artifacts: ArtifactStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.service, self.settings, self.source, self.telegram = (
            service,
            settings,
            source,
            telegram,
        )
        self.db, self.clock = service.db, clock
        self.artifacts = artifacts or ArtifactStore(settings, self.db)

    def check(self, job_id: str) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT state,cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            paused = connection.execute("SELECT paused FROM queue_control").fetchone()[0]
            if row["cancel_requested"]:
                connection.execute(
                    "UPDATE jobs SET state='cancelled',revision=revision+1 "
                    "WHERE id=? AND state<>'cancelled'",
                    (job_id,),
                )
        if paused or row["cancel_requested"] or row["state"] == "cancelled":
            raise AppError("JOB_INTERRUPTED", "Задание остановлено владельцем.", 409)

    def state(
        self,
        job_id: str,
        state: str,
        *,
        error: AppError | None = None,
        retry_at: float | None = None,
        page: int | None = None,
    ) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT state,review_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row["state"] == "cancelled":
                return
            review = json.loads(row["review_json"])
            if page is not None:
                review["page"] = page
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,error_message=?,retry_at=?, "
                "review_json=?,revision=revision+1 WHERE id=?",
                (
                    state,
                    error.code if error else None,
                    error.message if error else None,
                    retry_at,
                    json.dumps(review),
                    job_id,
                ),
            )

    async def run_once(self) -> bool:
        # This OS lock is held across the actual network call; an expired lease never permits
        # a second live sender. Lock loss through process death is reconciled below.
        lock_path = self.db.path.with_name("publisher-" + self.db.path.name)
        with source_lock(lock_path) as acquired:
            if not acquired:
                return False
            with self.db.transaction() as connection:
                uncertain = connection.execute(
                    "SELECT DISTINCT p.job_id FROM publication_parts p "
                    "JOIN jobs j ON j.id=p.job_id WHERE p.state='sending' "
                    "AND j.state NOT IN ('cancelled','completed')"
                ).fetchall()
                for item in uncertain:
                    connection.execute(
                        "UPDATE jobs SET state='needs_review',error_code="
                        "'DELIVERY_UNCERTAIN',error_message='Потерян результат отправки. "
                        "Сверьте канал.',"
                        "revision=revision+1 WHERE id=? AND state<>'needs_review'",
                        (item[0],),
                    )
                if connection.execute("SELECT paused FROM queue_control").fetchone()[0]:
                    return False
                row = connection.execute(
                    "SELECT * FROM jobs WHERE owner_id=? AND state NOT IN "
                    "('completed','cancelled','failed') ORDER BY sequence LIMIT 1",
                    (self.service.owner_id,),
                ).fetchone()
                if row is None or row["state"] in {"needs_review", "paused"}:
                    return False
                if row["retry_at"] is not None and row["retry_at"] > self.clock():
                    return False
                connection.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (row["id"],))
                job_id = row["id"]
            page = None
            try:
                await self._execute(job_id)
            except AppError as error:
                if error.code == "JOB_INTERRUPTED":
                    return True
                job = self.service.get_job(job_id)
                if error.code == "ANIMATION_REVIEW":
                    page = error.details.get("page")
                if error.code in SOURCE_PAUSES:
                    with self.db.transaction() as connection:
                        connection.execute(
                            "UPDATE source_control SET paused=1,reason_code=?", (error.code,)
                        )
                    state = "paused"
                elif error.code in {
                    "DELIVERY_UNCERTAIN",
                    "SOURCE_VERSION_CHANGED",
                    "ANIMATION_REVIEW",
                    "PUBLICATION_RECONFIRM_REQUIRED",
                }:
                    state = "needs_review"
                elif error.retryable and job["attempts"] < self.settings.job_max_attempts:
                    state = "retry_wait"
                else:
                    state = "paused" if job["published_parts"] else "failed"
                delay = max(
                    error.details.get("retry_after", 0), min(300, 5 * 2 ** min(job["attempts"], 6))
                )
                self.state(
                    job_id,
                    state,
                    error=error,
                    retry_at=self.clock() + delay if state == "retry_wait" else None,
                    page=page,
                )
            except Exception:
                # A failure after sending is intentionally left sending for reconciliation.
                with self.db.read() as connection:
                    sending = connection.execute(
                        "SELECT 1 FROM publication_parts WHERE job_id=? AND state='sending'",
                        (job_id,),
                    ).fetchone()
                failure = AppError(
                    "DELIVERY_UNCERTAIN" if sending else "INTERNAL_ERROR",
                    "Исполнение прервано. Требуется проверка владельца.",
                )
                partial = self.service.get_job(job_id)["published_parts"] > 0
                self.state(
                    job_id,
                    "needs_review" if sending else ("paused" if partial else "failed"),
                    error=failure,
                )
            return True

    async def _execute(self, job_id: str) -> None:
        job = self.service.get_job(job_id)
        if not job["plan"].get("parts"):
            raise AppError(
                "PUBLICATION_RECONFIRM_REQUIRED",
                "Создайте новый черновик и подтвердите серверную подпись.",
                409,
            )
        if job["published_pages"] == len(job["pages"]):
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET state='completed',completed_at=?,"
                    "revision=revision+1 WHERE id=?",
                    (self.clock(), job_id),
                )
            return
        draft = self.service.get_draft(job["draft_id"])

        def heartbeat() -> None:
            self.check(job_id)

        self.check(job_id)
        self.state(job_id, "downloading")
        with source_lock(self.db.path) as acquired:
            if not acquired:
                raise AppError("SOURCE_BUSY", "Источник занят.", 503, retryable=True)
            current = await asyncio.to_thread(self.source.inspect, draft["source_url"], heartbeat)
            expected = draft["snapshot"]["page_ids"]
            if len(current.page_ids) != len(expected) or any(
                current.page_ids[p["source_page_index"] - 1] != p["page_identity"]
                for p in job["pages"]
            ):
                raise AppError(
                    "SOURCE_VERSION_CHANGED",
                    "Состав источника изменился. Отмените задание и подтвердите новый черновик.",
                    409,
                )
            self.state(job_id, "preparing")
            for page in job["pages"]:
                self.check(job_id)
                index = page["source_page_index"]
                try:
                    await asyncio.to_thread(
                        self.artifacts.prepare_page,
                        self.source,
                        job_id,
                        index,
                        page["page_identity"],
                        heartbeat,
                        first_frame=index in job["review"].get("first_frames", []),
                    )
                except AppError as error:
                    error.details["page"] = index
                    raise
        # No channel sends until every selected page is fully downloaded and verified.
        await self.telegram.check_channel(self.service.channel_id)
        self.state(job_id, "publishing")
        for part in job["plan"]["parts"]:
            self.check(job_id)
            paths = [self.artifacts.path(job_id, page, "photo") for page in part["pages"]]
            if not all(self.artifacts.valid(job_id, page, "photo") for page in part["pages"]):
                raise AppError("MEDIA_INVALID", "Подготовленный файл изменился перед отправкой.")
            with self.db.transaction() as connection:
                self.check(job_id)
                connection.execute(
                    "INSERT OR IGNORE INTO publication_parts(job_id,part_index) VALUES (?,?)",
                    (job_id, part["index"]),
                )
                old = connection.execute(
                    "SELECT state FROM publication_parts WHERE job_id=? AND part_index=?",
                    (job_id, part["index"]),
                ).fetchone()
                if old["state"] == "sent":
                    continue
                connection.execute(
                    "UPDATE publication_parts SET state='sending',attempt_id=?,"
                    "attempted_at=? WHERE job_id=? AND part_index=?",
                    (str(uuid4()), utc_now(), job_id, part["index"]),
                )
            try:
                result = await self.telegram.send_part(
                    self.service.channel_id, part, paths, job["plan"]["options"]
                )
            except AppError as error:
                if error.code != "DELIVERY_UNCERTAIN":
                    with self.db.transaction() as connection:
                        connection.execute(
                            "UPDATE publication_parts SET state='pending' "
                            "WHERE job_id=? AND part_index=?",
                            (job_id, part["index"]),
                        )
                raise
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE publication_parts SET state='sent',result_json=? "
                    "WHERE job_id=? AND part_index=?",
                    (json.dumps(result), job_id, part["index"]),
                )
                connection.execute(
                    "UPDATE jobs SET published_pages=published_pages+?, "
                    "published_parts=published_parts+1,revision=revision+1 WHERE id=?",
                    (len(part["pages"]), job_id),
                )
        self.check(job_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET state='completed',completed_at=?,revision=revision+1 WHERE id=?",
                (self.clock(), job_id),
            )
        self.artifacts.cleanup()
