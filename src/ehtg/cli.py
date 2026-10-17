import argparse
import json
import signal
from pathlib import Path

from ehtg.analysis import AnalysisQueue
from ehtg.auth import DeviceAuth
from ehtg.domain.errors import AppError
from ehtg.settings import Settings
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.locking import source_lock
from ehtg.source.session import SessionStore
from ehtg.storage.database import Database
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
    elif args.command in {"source", "worker"}:
        try:
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
        if args.once:
            worker.run_once()
        else:
            signal.signal(signal.SIGTERM, signal.default_int_handler)
            worker.run()
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


if __name__ == "__main__":
    main()
