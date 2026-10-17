import hashlib
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.resources import files
from pathlib import Path
from uuid import uuid4


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._active: ContextVar[sqlite3.Connection | None] = ContextVar(
            "database_transaction", default=None
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        active = self._active.get()
        if active is not None:
            yield active
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = self._active.get()
        if active is not None:
            name = "nested_" + uuid4().hex
            active.execute("SAVEPOINT " + name)
            try:
                yield active
                active.execute("RELEASE SAVEPOINT " + name)
            except BaseException:
                active.execute("ROLLBACK TO SAVEPOINT " + name)
                active.execute("RELEASE SAVEPOINT " + name)
                raise
            return
        with self.read() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._active.set(connection)
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._active.reset(token)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.read() as connection:
            if sys.platform != "win32":
                self.path.chmod(0o600)
            connection.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, checksum TEXT NOT NULL)"
            )
            migrations = sorted(
                files("ehtg.storage").joinpath("migrations").iterdir(),
                key=lambda resource: resource.name,
            )
            known_names = {migration.name for migration in migrations}
            applied = {
                row["name"]: row["checksum"]
                for row in connection.execute("SELECT name, checksum FROM schema_migrations")
            }
            if applied.keys() - known_names:
                raise RuntimeError("Database schema is newer than this application")
            for migration in migrations:
                sql = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if migration.name in applied:
                    if applied[migration.name] != checksum:
                        raise RuntimeError(f"Applied migration changed: {migration.name}")
                    continue
                # executescript commits implicitly. Keep the whole migration in our transaction.
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Incomplete migration: {migration.name}")
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", (migration.name, checksum)
                )

    def backup(self, destination: Path) -> None:
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.read() as source, sqlite3.connect(destination) as target:
            if sys.platform != "win32":
                destination.chmod(0o600)
            source.backup(target)
