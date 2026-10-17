from pathlib import Path

import pytest

from ehtg.service import GalleryService
from ehtg.storage.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def service(database: Database) -> GalleryService:
    return GalleryService(database, "123456", "-1001234567890")
