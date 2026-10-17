import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.service import GalleryService
from ehtg.storage.database import Database


def ready_draft(service: GalleryService, gid: int = 123) -> dict:
    draft = service.create_draft(f"https://e-hentai.org/g/{gid}/abcdef0123/", PageSelection())
    # Synthetic adapter fixture; no network or live gallery is used in tests.
    service.record_analysis(
        draft["id"],
        draft["revision"],
        GallerySnapshot("Synthetic gallery", ("page-a", "same-image", "same-image")),
    )
    return service.get_draft(draft["id"])


def test_migrations_are_repeatable_and_durable(database: Database) -> None:
    database.initialize()
    with database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4


def test_snapshot_is_immutable_and_preserves_repeated_positions(service: GalleryService) -> None:
    draft = ready_draft(service)
    with service.db.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE gallery_snapshots SET metadata_json='{}'")
    result = service.publish(draft["id"], draft["revision"], "one")
    pages = service.get_job(result["job_id"])["pages"]
    assert [page["source_page_index"] for page in pages] == [1, 2, 3]
    assert pages[1]["page_identity"] == pages[2]["page_identity"]


def test_edit_invalidates_old_confirmation(service: GalleryService) -> None:
    draft = ready_draft(service)
    changed = service.edit_selection(draft["id"], draft["revision"], PageSelection("1-2"))
    assert changed["revision"] == draft["revision"] + 1
    with pytest.raises(AppError) as error:
        service.publish(draft["id"], draft["revision"], "stale")
    assert error.value.code == "STALE_REVISION"
    assert service.list_jobs()["items"] == []


def test_lost_response_is_recovered_after_restart(service: GalleryService) -> None:
    draft = ready_draft(service)
    first = service.publish(draft["id"], draft["revision"], "lost-response")
    restarted = GalleryService(Database(service.db.path), service.owner_id, service.channel_id)
    assert restarted.publish(draft["id"], draft["revision"], "lost-response") == first
    with pytest.raises(AppError) as error:
        restarted.publish(draft["id"], draft["revision"] + 1, "lost-response")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(restarted.list_jobs()["items"]) == 1


def test_concurrent_confirmations_make_one_job(service: GalleryService) -> None:
    draft = ready_draft(service)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: service.publish(draft["id"], draft["revision"], "double-click"), range(8)
            )
        )
    assert len({result["job_id"] for result in results}) == 1
    assert len(service.list_jobs()["items"]) == 1


def test_two_interfaces_cannot_queue_same_gallery(service: GalleryService) -> None:
    first, second = ready_draft(service), ready_draft(service)

    def publish(draft: dict) -> str:
        try:
            return service.publish(draft["id"], draft["revision"], draft["id"])["job_id"]
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, [first, second]))
    assert results.count("ALREADY_QUEUED") == 1
    assert len(service.list_jobs()["items"]) == 1


def test_failure_rolls_back_entire_confirmation(service: GalleryService) -> None:
    draft = ready_draft(service)
    with service.db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        service.publish(draft["id"], draft["revision"], "rollback")
    with service.db.read() as connection:
        for table in ("jobs", "job_pages", "idempotency"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert service.get_draft(draft["id"])["state"] == "ready"


def test_unanalyzed_draft_cannot_be_published(service: GalleryService) -> None:
    draft = service.create_draft("https://e-hentai.org/g/123/abcdef0123/", PageSelection())
    with pytest.raises(AppError) as error:
        service.publish(draft["id"], draft["revision"], "early")
    assert error.value.code == "DRAFT_NOT_READY"


def test_late_analysis_cannot_replace_confirmed_snapshot(service: GalleryService) -> None:
    draft = ready_draft(service)
    service.publish(draft["id"], draft["revision"], "confirm")
    with pytest.raises(AppError):
        service.record_analysis(draft["id"], 1, GallerySnapshot("Changed", ("new",)))


def test_pagination_is_fifo(service: GalleryService) -> None:
    job_ids = []
    for gid in range(1, 4):
        draft = ready_draft(service, gid)
        job_ids.append(service.publish(draft["id"], draft["revision"], str(gid))["job_id"])
    first = service.list_jobs(limit=2)
    second = service.list_jobs(after=first["next_cursor"], limit=2)
    assert [item["id"] for item in first["items"] + second["items"]] == job_ids
    assert second["next_cursor"] is None


def test_backup_restores_confirmed_queue(service: GalleryService, tmp_path: Path) -> None:
    draft = ready_draft(service)
    job = service.publish(draft["id"], draft["revision"], "backup")
    target = tmp_path / "backup.sqlite3"
    service.db.backup(target)
    restored = GalleryService(Database(target), service.owner_id, service.channel_id)
    restored.db.initialize()
    assert restored.get_job(job["job_id"])["state"] == "queued"
    assert restored.publish(draft["id"], draft["revision"], "backup") == job
    with pytest.raises(ValueError):
        service.db.backup(target)


def test_queue_limit_rejects_confirmation_without_partial_write(database: Database) -> None:
    service = GalleryService(database, "123456", "-1001234567890", max_queue=1)
    first = ready_draft(service, 1)
    service.publish(first["id"], first["revision"], "first")
    second = ready_draft(service, 2)
    with pytest.raises(AppError) as error:
        service.publish(second["id"], second["revision"], "queue-full")
    assert error.value.code == "RESOURCE_LIMIT"
    assert service.get_draft(second["id"])["state"] == "ready"
    assert len(service.list_jobs()["items"]) == 1


def test_changed_migration_is_rejected(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='changed' WHERE name='001_core.sql'"
        )
    with pytest.raises(RuntimeError, match="Applied migration changed"):
        database.initialize()


def test_newer_database_cannot_be_opened_by_older_application(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute("INSERT INTO schema_migrations VALUES ('999_future.sql','future')")
    with pytest.raises(RuntimeError, match="newer than this application"):
        database.initialize()
