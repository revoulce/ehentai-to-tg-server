import argparse
import asyncio
import json
import signal
import time
from pathlib import Path

from ehtg.analysis import AnalysisQueue
from ehtg.artifacts import ArtifactStore
from ehtg.auth import DeviceAuth
from ehtg.domain.errors import AppError
from ehtg.publisher import PublicationWorker
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.locking import source_lock
from ehtg.source.session import SessionStore
from ehtg.storage.database import Database
from ehtg.telegram import TelegramClient
from ehtg.worker import AnalysisWorker


def main() -> None:
    parser = argparse.ArgumentParser(prog="ehtg")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Apply checked database migrations")
    commands.add_parser("pair", help="Print a single-use pairing code valid for five minutes")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    schema = commands.add_parser("export-openapi", help="Write the current HTTP contract")
    schema.add_argument("destination", type=Path)
    worker = commands.add_parser("worker", help="Run the persistent draft analysis worker")
    worker.add_argument("--once", action="store_true", help="Process at most one draft and exit")
    worker.add_argument("--analysis-only", action="store_true")
    commands.add_parser("bot", help="Run the owner-only Telegram long-polling bot")
    commands.add_parser("cleanup", help="Remove expired successful media files")
    commands.add_parser("check-telegram", help="Verify bot identity and channel posting permission")
    source = commands.add_parser(
        "source", help="Inspect source compatibility and manage its session"
    )
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("probe", help="Check the pinned extractor without network access")
    source_commands.add_parser("status", help="Show safe local source/session status")
    cookie_import = source_commands.add_parser(
        "import-cookies", help="Verify and atomically import a Netscape cookie jar"
    )
    cookie_import.add_argument("path", type=Path)
    source_commands.add_parser(
        "resume", help="Verify the installed session and resume paused analysis"
    )
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_path)
    if args.command == "export-openapi":
        from ehtg.api.app import create_app

        args.destination.write_text(
            json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    database.initialize()
    if args.command == "pair":
        print(DeviceAuth(database, settings.owner_id, settings.allowed_origins).issue_code())
    elif args.command == "backup":
        database.backup(args.destination)
    elif args.command in {"source", "worker", "bot", "cleanup", "check-telegram"}:
        try:
            if args.command == "cleanup":
                print(json.dumps({"removed": ArtifactStore(settings, database).cleanup()}))
            elif args.command in {"bot", "check-telegram"}:
                asyncio.run(_telegram_command(settings, database, args.command))
            else:
                _source_command(settings, database, args)
        except AppError as error:
            parser.exit(2, f"{error.code}: {error.message}\n")
        except KeyboardInterrupt:
            return


def _source_command(settings: Settings, database: Database, args: argparse.Namespace) -> None:
    queue = AnalysisQueue(
        database,
        settings.owner_id,
        lease_seconds=settings.analysis_lease_seconds,
        max_attempts=settings.analysis_max_attempts,
        retry_seconds=settings.analysis_retry_seconds,
        max_pages=settings.max_pages,
        max_queue=settings.max_queue,
    )
    source = GalleryDLSource(settings)
    if args.command == "worker":
        if not settings.source_enabled:
            raise AppError(
                "SOURCE_DISABLED", "Включите EHTG_SOURCE_ENABLED перед запуском worker.", 503
            )
        source.probe()
        worker = AnalysisWorker(queue, source)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        asyncio.run(_workers(settings, database, worker, source, args.once, args.analysis_only))
        return
    if args.source_command == "probe":
        print(json.dumps(source.probe()))
        return
    if args.source_command == "status":
        with database.read() as connection:
            control = dict(
                connection.execute("SELECT paused,reason_code FROM source_control").fetchone()
            )
        print(
            json.dumps(
                {
                    "enabled": settings.source_enabled,
                    "control": control,
                    "session": SessionStore(settings.source_session_path).info(),
                }
            )
        )
        return
    with source_lock(database.path) as acquired:
        if not acquired:
            raise AppError(
                "SOURCE_BUSY",
                "Источник сейчас занят анализом. Повторите команду позже.",
                409,
                retryable=True,
            )
        store = SessionStore(settings.source_session_path)
        if args.source_command == "import-cookies":
            store.import_cookies(args.path, source.verify)
            print(json.dumps({"state": "installed"}))
        elif args.source_command == "resume":
            source.verify(store.load())
            queue.resume_source()
            print(json.dumps({"state": "resumed"}))


async def _workers(
    settings: Settings,
    database: Database,
    analysis: AnalysisWorker,
    source: GalleryDLSource,
    once: bool,
    analysis_only: bool,
) -> None:
    telegram = (
        TelegramClient(settings.bot_token.get_secret_value())
        if settings.bot_token is not None and not analysis_only
        else None
    )
    service = GalleryService(
        database,
        settings.owner_id,
        settings.channel_id,
        max_pages=settings.max_pages,
        max_queue=settings.max_queue,
    )
    publisher = (
        PublicationWorker(service, settings, source, telegram) if telegram is not None else None
    )
    next_cleanup = 0.0
    try:
        while True:
            if publisher is not None and time.monotonic() >= next_cleanup:
                await asyncio.to_thread(publisher.artifacts.cleanup)
                next_cleanup = time.monotonic() + 3600
            did_work = await asyncio.to_thread(analysis.run_once)
            if publisher is not None:
                did_work = await publisher.run_once() or did_work
            if once:
                break
            if not did_work:
                await asyncio.sleep(1)
    finally:
        if telegram is not None:
            await telegram.close()


async def _telegram_command(settings: Settings, database: Database, command: str) -> None:
    if settings.bot_token is None:
        raise AppError("TELEGRAM_NOT_CONFIGURED", "Задайте EHTG_BOT_TOKEN на сервере.", 503)
    client = TelegramClient(settings.bot_token.get_secret_value())
    try:
        if command == "check-telegram":
            await client.check_channel(settings.channel_id)
            print(json.dumps({"telegram": "ready"}))
        else:
            from ehtg.bot import OwnerBot

            signal.signal(signal.SIGTERM, signal.default_int_handler)
            await OwnerBot(settings, database, client).run()
    finally:
        await client.close()


if __name__ == "__main__":
    main()
