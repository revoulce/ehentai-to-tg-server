"""Owner-only bot. Inbound commands, domain changes and response intents commit together."""

import asyncio
import json
import re
import time
from dataclasses import replace
from typing import Any
from uuid import UUID

from ehtg.analysis import AnalysisQueue
from ehtg.artifacts import ArtifactStore
from ehtg.auth import DeviceAuth
from ehtg.domain.errors import AppError
from ehtg.domain.publication import PublicationOptions, page_ranges
from ehtg.domain.selection import PageSelection
from ehtg.publisher import JobControl
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source.locking import source_lock
from ehtg.source.session import SessionStore
from ehtg.storage.database import Database
from ehtg.telegram import TelegramClient

HELP = (
    "Отправьте ссылку E-Hentai. Дождитесь карточки и подтвердите точную ревизию.\n"
    "/draft ID — предпросмотр\n"
    "/pages ID REV 1-20,25 | 5,12-14 — включения | исключения; * — все\n"
    "/title ID REV название; /caption ID REV текст; /caption ID REV * — шаблон\n"
    "/tags ID REV namespace:tag,namespace:tag — скрыть теги\n"
    "/options ID REV silent=1 spoiler=0\n"
    "/publish ID REV — подтвердить\n"
    "/queue /history /status [JOB] /source_status\n"
    "/cancel JOB REV /retry JOB REV /pause /resume\n"
    "/review JOB REV sent ссылка — уже отправлено\n"
    "/review JOB REV not_sent — не отправлено, разрешить повтор\n"
    "/frame JOB REV PAGE — использовать первый кадр\n"
    "/preview JOB PAGE — показать подготовленную страницу/первый кадр\n"
    "/exclude JOB REV PAGE — исключить анимацию\n"
    "/repost JOB — новый черновик для явного повтора\n"
    "/analysis_retry ID REV /analysis_cancel ID REV\n"
    "/pair /devices /revoke DEVICE\n"
    "Публикации по умолчанию без звука и без spoiler. "
    "Решения сверки принимайте после проверки канала."
)


class OwnerBot:
    def __init__(self, settings: Settings, database: Database, client: TelegramClient) -> None:
        self.settings, self.db, self.client = settings, database, client
        self.service = GalleryService(
            database,
            settings.owner_id,
            settings.channel_id,
            max_pages=settings.max_pages,
            max_queue=settings.max_queue,
        )
        self.control = JobControl(self.service)
        self.auth = DeviceAuth(database, settings.owner_id, settings.allowed_origins)
        self.analysis = AnalysisQueue(
            database, settings.owner_id, max_pages=settings.max_pages, max_queue=settings.max_queue
        )

    def authorized(self, update: dict[str, Any]) -> bool:
        try:
            callback = update.get("callback_query")
            event = callback or update["message"]
            message = callback["message"] if callback else event
            return (
                str(event["from"]["id"]) == self.settings.owner_id
                and message["chat"]["type"] == "private"
                and str(message["chat"]["id"]) == self.settings.owner_id
            )
        except (KeyError, TypeError):
            return False

    def enqueue(
        self,
        key: str,
        text: str,
        *,
        markup: dict[str, Any] | None = None,
        method: str = "sendMessage",
        extra: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "chat_id": self.settings.owner_id,
            "text": text[:4000],
            "link_preview_options": {"is_disabled": True},
        }
        if markup is not None:
            data["reply_markup"] = markup
        if extra:
            data.update(extra)
        if method == "answerCallbackQuery":
            data = {"callback_query_id": data["callback_query_id"], "text": text}
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO bot_outbox(id,payload_json) VALUES (?,?)",
                (key, json.dumps({"method": method, "data": data}, ensure_ascii=False)),
            )

    def process_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < 0:
            raise AppError("INVALID_UPDATE", "Некорректный Telegram update.")
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT processed FROM bot_inbox WHERE update_id=?", (update_id,)
            ).fetchone()
            if row is not None and row["processed"]:
                return
            # Untrusted users do not enter the domain layer or persist their message contents.
            connection.execute("INSERT OR IGNORE INTO bot_inbox VALUES (?,?,0)", (update_id, "{}"))
            if self.authorized(update):
                callback = update.get("callback_query")
                try:
                    if callback:
                        match = re.fullmatch(
                            r"d:([0-9a-f]{32}):([1-9][0-9]{0,8}):([prc])", callback.get("data", "")
                        )
                        if match is None:
                            raise AppError(
                                "INVALID_CALLBACK", "Кнопка устарела. Откройте /draft ID."
                            )
                        draft_id, rev, action = str(UUID(match[1])), int(match[2]), match[3]
                        if action == "p":
                            receipt = self.service.publish(draft_id, rev, f"bot-{update_id}")
                            self.enqueue(
                                f"update:{update_id}",
                                f"Сервер принял задание {receipt['job_id']}. "
                                "Результат доступен в /status.",
                            )
                        else:
                            if action == "c":
                                self.analysis.cancel(draft_id, rev)
                            self.draft_card(draft_id, f"update:{update_id}")
                        self.enqueue(
                            f"callback:{update_id}",
                            "",
                            method="answerCallbackQuery",
                            extra={"callback_query_id": callback["id"]},
                        )
                    else:
                        text = update["message"].get("text", "").strip()
                        self.command(text, update_id)
                except AppError as error:
                    self.enqueue(f"update:{update_id}", f"{error.code}: {error.message}")
                except (ValueError, TypeError, KeyError, IndexError):
                    self.enqueue(
                        f"update:{update_id}", "Проверьте команду, ID и номер ревизии. /help"
                    )
            connection.execute("UPDATE bot_inbox SET processed=1 WHERE update_id=?", (update_id,))
            connection.execute(
                "UPDATE bot_state SET next_offset=MAX(next_offset,?)", (update_id + 1,)
            )

    def draft_card(self, draft_id: str, key: str) -> None:
        draft = self.service.get_draft(draft_id)
        preview = draft["preview"]
        text = (
            preview["parts"][0]["caption"]
            if preview
            else f"Анализ: {draft['analysis']['state']}. {draft['analysis']['error_message'] or ''}"
        )
        text += f"\n\nЧерновик {draft_id}\nРевизия {draft['revision']}"
        if draft["preview_error"]:
            text += "\n" + draft["preview_error"]["message"]
        if draft["duplicate"]:
            text += (
                f"\nПредыдущее задание: {draft['duplicate']['id']} ({draft['duplicate']['state']})."
            )
        if preview:
            text += f"\nЧастей: {len(preview['parts'])}. Скрыто тегов: {preview['hidden_tags']}."
        if draft["snapshot"] and draft["snapshot"].get("canonical_url"):
            text += "\nСсылка на изображение преобразована в черновик всей галереи. "
            text += "Проверьте выбранные страницы."
        prefix = f"d:{UUID(draft_id).hex}:{draft['revision']}:"
        buttons = [{"text": "Обновить", "callback_data": prefix + "r"}]
        if draft["state"] == "ready" and preview:
            buttons.append({"text": "Опубликовать", "callback_data": prefix + "p"})
        elif draft["state"] == "analyzing":
            buttons.append({"text": "Отменить анализ", "callback_data": prefix + "c"})
        self.enqueue(key, text, markup={"inline_keyboard": [buttons]})
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO bot_drafts VALUES (?,?) ON CONFLICT(draft_id) DO UPDATE "
                "SET shown_revision=excluded.shown_revision",
                (draft_id, draft["revision"]),
            )

    def command(self, text: str, update_id: int) -> None:
        key = f"update:{update_id}"
        if len(text) > 8192:
            raise AppError("RESOURCE_LIMIT", "Слишком длинная команда.")
        if not text.startswith("/"):
            draft = self.service.create_draft(text, PageSelection())
            self.draft_card(draft["id"], key)
            return
        name, _, arguments = text.partition(" ")
        name = name.split("@")[0]
        args = arguments.strip().split(maxsplit=2)
        if name in {"/start", "/help"}:
            self.enqueue(key, HELP)
        elif name == "/pair":
            code = self.auth.issue_code()
            self.enqueue(
                key, f"Код сопряжения (5 минут): {code}\nВведите его в настройках расширения."
            )
        elif name == "/draft":
            self.draft_card(str(UUID(args[0])), key)
        elif name == "/publish":
            result = self.service.publish(str(UUID(args[0])), int(args[1]), f"bot-{update_id}")
            self.enqueue(key, f"Сервер принял задание {result['job_id']}.")
        elif name in {"/pages", "/title", "/caption", "/options", "/tags"}:
            draft_id, rev, value = str(UUID(args[0])), int(args[1]), args[2]
            draft = self.service.get_draft(draft_id)
            selection = PageSelection(**draft["page_selection"])
            options = PublicationOptions(**draft["options"])
            if name == "/pages":
                include, _, exclude = value.partition("|")
                selection = PageSelection(
                    "" if include.strip() == "*" else include.strip(), exclude.strip()
                )
            elif name == "/title":
                options = replace(options, title=None if value == "*" else value)
            elif name == "/caption":
                options = replace(options, caption=None if value == "*" else value)
            elif name == "/tags":
                options = replace(options, removed_tags=[v.strip() for v in value.split(",")])
            else:
                flags = dict(flag.split("=") for flag in value.split())
                if set(flags) != {"silent", "spoiler"} or any(
                    v not in {"0", "1"} for v in flags.values()
                ):
                    raise ValueError
                options = replace(
                    options,
                    disable_notification=flags["silent"] == "1",
                    has_spoiler=flags["spoiler"] == "1",
                )
            self.service.edit_selection(draft_id, rev, selection, options)
            self.draft_card(draft_id, key)
        elif name in {"/pause", "/resume"}:
            result = self.control.pause(name == "/pause")
            self.enqueue(key, "Очередь на паузе." if result["paused"] else "Очередь продолжена.")
        elif name in {"/cancel", "/retry"}:
            result = self.control.action(str(UUID(args[0])), int(args[1]), name[1:])
            self.enqueue(key, self.job_text(result))
        elif name in {"/review", "/frame", "/exclude"}:
            job_id, rev = str(UUID(args[0])), int(args[1])
            decision, _, evidence = args[2].partition(" ")
            result = self.control.review(
                job_id,
                rev,
                ("first_frame" if name == "/frame" else "exclude")
                if name != "/review"
                else decision,
                evidence=evidence,
                page=int(args[2]) if name != "/review" else None,
            )
            self.enqueue(key, self.job_text(result))
        elif name == "/repost":
            draft = self.service.repost(str(UUID(args[0])))
            self.draft_card(draft["id"], key)
        elif name == "/preview":
            job_id, page = str(UUID(args[0])), int(args[1])
            job = self.service.get_job(job_id)
            if page not in [p["source_page_index"] for p in job["pages"]]:
                raise AppError("NOT_FOUND", "Страница не найдена.", 404)
            if not ArtifactStore(self.settings, self.db).path(job_id, page, "photo").is_file():
                raise AppError("NOT_FOUND", "Предпросмотр ещё не подготовлен.", 404)
            self.enqueue(
                key,
                f"Предпросмотр страницы {page}",
                method="previewPhoto",
                extra={"job_id": job_id, "page": page},
            )
        elif name in {"/analysis_retry", "/analysis_cancel"}:
            draft_id, rev = str(UUID(args[0])), int(args[1])
            if name == "/analysis_retry":
                self.analysis.retry(draft_id, rev)
            else:
                self.analysis.cancel(draft_id, rev)
            self.draft_card(draft_id, key)
        elif name in {"/queue", "/history", "/status"}:
            if name == "/status" and args:
                self.enqueue(key, self.job_text(self.service.get_job(str(UUID(args[0])))))
            else:
                with self.db.read() as connection:
                    rows = connection.execute(
                        "SELECT id,state,revision,published_pages FROM jobs "
                        "WHERE owner_id=? ORDER BY sequence DESC LIMIT 20",
                        (self.settings.owner_id,),
                    ).fetchall()
                    paused = connection.execute("SELECT paused FROM queue_control").fetchone()[0]
                self.enqueue(
                    key,
                    ("Очередь на паузе.\n" if paused else "")
                    + (
                        "\n".join(
                            f"{r['id']} · {r['state']} · rev {r['revision']} · "
                            f"{r['published_pages']} стр."
                            for r in rows
                        )
                        or "Очередь пуста."
                    ),
                )
        elif name == "/source_status":
            with self.db.read() as connection:
                control = dict(
                    connection.execute("SELECT paused,reason_code FROM source_control").fetchone()
                )
            self.enqueue(
                key,
                json.dumps(
                    {
                        "enabled": self.settings.source_enabled,
                        **control,
                        "session": SessionStore(self.settings.source_session_path).info(),
                    },
                    ensure_ascii=False,
                ),
            )
        elif name == "/devices":
            self.enqueue(
                key,
                "\n".join(
                    f"{d['id']} · {d['name']} · {'отозван' if d['revoked_at'] else 'активен'}"
                    for d in self.auth.devices()
                )
                or "Устройств нет.",
            )
        elif name == "/revoke":
            self.auth.revoke(str(UUID(args[0])))
            self.enqueue(key, "Доступ устройства отозван.")
        else:
            self.enqueue(key, "Неизвестная команда. /help")

    def job_text(self, job: dict[str, Any]) -> str:
        parts = job["plan"].get("parts", [])
        title = parts[0]["caption"].split("\n", 1)[0] if parts else ""
        text = (
            f"{title}\n{job['id']}\n{job['state']} · ревизия {job['revision']}\n"
            f"Страниц: {job['published_pages']}/{len(job['pages'])}; "
            f"частей: {job['published_parts']}/{len(job['plan'].get('parts', []))}"
        )
        if job["error_code"]:
            text += f"\n{job['error_code']}: {job['error_message']}"
        if job["retry_at"]:
            text += (
                f"\nПовтор UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(job['retry_at']))}"
            )
        if job["review"].get("page"):
            text += f"\nСтраница для решения: {job['review']['page']}"
        for part in job["parts"]:
            if part["state"] == "sending":
                text += f"\nСверьте часть {part['part_index']}, попытка {part['attempted_at']}."
                pages = parts[part["part_index"] - 1]["pages"]
                text += f" Страницы {page_ranges(pages)}."
            if part["result"]:
                text += "\n" + part["result"][0].get("link", "")
        return text

    def collect_notifications(self) -> None:
        with self.db.transaction() as connection:
            ready = connection.execute(
                "SELECT d.id FROM bot_drafts b JOIN drafts d ON d.id=b.draft_id "
                "WHERE d.revision>b.shown_revision AND d.state IN ('ready','error')"
            ).fetchall()
            for row in ready:
                draft = self.service.get_draft(row["id"])
                self.draft_card(row["id"], f"draft:{row['id']}:{draft['revision']}")
            jobs = connection.execute(
                "SELECT id,state,error_code FROM jobs WHERE owner_id=? "
                "AND state IN ('completed','failed','paused','needs_review')",
                (self.settings.owner_id,),
            ).fetchall()
            for job in jobs:
                self.enqueue(
                    f"job:{job['id']}:{job['state']}:{job['error_code']}",
                    self.job_text(self.service.get_job(job["id"])),
                )
            pauses = connection.execute(
                "SELECT e.sequence,e.object_id FROM events e JOIN draft_analysis a "
                "ON a.draft_id=e.object_id WHERE e.kind='analysis_paused' AND a.state='paused' "
                "AND e.sequence=(SELECT MAX(sequence) FROM events WHERE object_id=e.object_id "
                "AND kind='analysis_paused')"
            ).fetchall()
            for pause in pauses:
                draft = self.service.get_draft(pause["object_id"])
                reason = draft["analysis"]["error_code"] or "проверьте /source_status"
                self.enqueue(
                    f"analysis-pause:{pause['sequence']}",
                    f"Источник приостановлен: {reason}. "
                    "После проверки сессии администратор выполняет ehtg source resume.",
                )

    async def flush_one(self) -> bool:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM bot_outbox WHERE state='pending' AND retry_at<=? "
                "ORDER BY rowid LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE bot_outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row["id"],)
            )
        payload = json.loads(row["payload_json"])
        try:
            if payload["method"] == "previewPhoto":
                data = payload["data"]
                path = ArtifactStore(self.settings, self.db).path(
                    data["job_id"], data["page"], "photo"
                )
                if not path.is_file():
                    raise AppError("NOT_FOUND", "Предпросмотр уже удалён.", 404)
                with path.open("rb") as handle:
                    await self.client.call(
                        "sendPhoto",
                        {"chat_id": self.settings.owner_id, "caption": data["text"]},
                        files={"photo": ("preview.jpg", handle, "image/jpeg")},
                        sending=True,
                    )
            else:
                await self.client.call(payload["method"], payload["data"], sending=True)
        except AppError as error:
            safe_retry = error.retryable and row["attempts"] < 5
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE bot_outbox SET state=?,retry_at=? WHERE id=?",
                    (
                        "pending" if safe_retry else "uncertain",
                        time.time() + max(30, error.details.get("retry_after", 0)),
                        row["id"],
                    ),
                )
        else:
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE bot_outbox SET state='sent',payload_json='{}' WHERE id=?", (row["id"],)
                )
        return True

    async def run(self) -> None:
        with source_lock(self.db.path.with_name("bot-" + self.db.path.name)) as acquired:
            if not acquired:
                raise AppError("BOT_BUSY", "Другой процесс бота уже работает.", 409)
            with self.db.transaction() as connection:
                connection.execute("UPDATE bot_outbox SET state='uncertain' WHERE state='sending'")
            while True:
                self.collect_notifications()
                for _ in range(10):
                    if not await self.flush_one():
                        break
                with self.db.read() as connection:
                    offset = connection.execute("SELECT next_offset FROM bot_state").fetchone()[0]
                try:
                    updates = await self.client.call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 20,
                            "limit": 20,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                    for update in updates:
                        self.process_update(update)
                except AppError as error:
                    await asyncio.sleep(max(5, min(60, error.details.get("retry_after", 5))))
