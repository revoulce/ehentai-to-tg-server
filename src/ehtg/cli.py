import argparse
import json
from pathlib import Path

from ehtg.auth import DeviceAuth
from ehtg.settings import Settings
from ehtg.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="ehtg")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Apply checked database migrations")
    commands.add_parser("pair", help="Print a single-use pairing code valid for five minutes")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    schema = commands.add_parser("export-openapi", help="Write the current HTTP contract")
    schema.add_argument("destination", type=Path)
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


if __name__ == "__main__":
    main()
