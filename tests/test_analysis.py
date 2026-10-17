from concurrent.futures import ThreadPoolExecutor

import pytest

from ehtg.analysis import AnalysisQueue
from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.service import GalleryService
from ehtg.source.locking import source_lock
from ehtg.storage.database import Database
from ehtg.worker import AnalysisWorker

SOURCE = "https://e-hentai.org/g/123/abcdef0123/"
SNAPSHOT = GallerySnapshot("Synthetic gallery", ("page-1", "page-2"))


def test_draft_and_pending_analysis_are_one_transaction(service: GalleryService) -> None:
    draft = service.create_draft(SOURCE, PageSelection())
    assert draft["analysis"]["state"] == "pending"
    assert AnalysisQueue(service.db, service.owner_id).claim("worker").draft_id == draft["id"]


def test_concurrent_claims_have_one_owner(service: GalleryService) -> None:
    for _ in range(2):
        service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id)
    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(queue.claim, ["worker-1", "worker-2", "worker-3", "worker-4"]))
    assert sum(lease is not None for lease in leases) == 1


def test_expired_attempt_cannot_write_or_renew(service: GalleryService) -> None:
    clock = [1000.0]
    draft = service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id, clock=lambda: clock[0])
    old = queue.claim("old")
    clock[0] += 30
    with pytest.raises(AppError):
        queue.heartbeat(old)
    current = queue.claim("new")
    assert current.generation == old.generation + 1
    for action in (
        lambda: queue.complete(old, SNAPSHOT),
        lambda: queue.fail(old, AppError("EXTRACTOR_INCOMPATIBLE", "Old")),
    ):
        with pytest.raises(AppError) as error:
            action()
        assert error.value.code == "ANALYSIS_LEASE_LOST"
    with pytest.raises(AppError):
        service.record_analysis(draft["id"], 1, SNAPSHOT)
    queue.complete(current, SNAPSHOT)
    assert service.get_draft(draft["id"])["state"] == "ready"


def test_safe_retry_is_persistent_and_has_budget(service: GalleryService) -> None:
    clock = [1000.0]
    draft = service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id, clock=lambda: clock[0], max_attempts=2)
    lease = queue.claim("first")
    network = AppError("SOURCE_NETWORK_UNAVAILABLE", "Сеть недоступна.", retryable=True)
    queue.fail(lease, network)
    assert queue.status(draft["id"])["retry_at"] == 1005
    assert queue.claim("too-early") is None
    restarted = AnalysisQueue(
        Database(service.db.path), service.owner_id, clock=lambda: clock[0], max_attempts=2
    )
    clock[0] = 1005
    restarted.fail(restarted.claim("second"), network)
    assert restarted.status(draft["id"])["state"] == "failed"
    assert service.get_draft(draft["id"])["state"] == "error"
    assert restarted.claim("third") is None


def test_crashes_consume_retry_budget(service: GalleryService) -> None:
    clock = [1000.0]
    draft = service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id, clock=lambda: clock[0], max_attempts=1)
    assert queue.claim("crashed") is not None
    clock[0] += 31
    assert queue.claim("recovery") is None
    assert queue.status(draft["id"])["error_code"] == "ANALYSIS_RETRIES_EXHAUSTED"


def test_source_failure_pauses_all_analysis(service: GalleryService) -> None:
    first = service.create_draft(SOURCE, PageSelection())
    service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id)
    queue.fail(queue.claim("worker"), AppError("SOURCE_AUTH_REQUIRED", "Требуется сессия."))
    assert queue.status(first["id"])["state"] == "paused"
    assert queue.claim("blocked") is None
    queue.resume_source()
    assert queue.claim("resumed").draft_id == first["id"]


def test_cancel_invalidates_live_lease_and_retry_changes_revision(service: GalleryService) -> None:
    draft = service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id)
    old = queue.claim("worker")
    queue.cancel(draft["id"], 1)
    with pytest.raises(AppError):
        queue.complete(old, SNAPSHOT)
    assert service.get_draft(draft["id"])["revision"] == 2
    with pytest.raises(AppError):
        queue.retry(draft["id"], 1)
    queue.retry(draft["id"], 2)
    current = queue.claim("again")
    queue.complete(current, SNAPSHOT)
    assert service.get_draft(draft["id"])["revision"] == 4


def test_source_lock_survives_expired_database_lease(service: GalleryService) -> None:
    with source_lock(service.db.path) as acquired:
        assert acquired
        with source_lock(service.db.path) as second:
            assert not second
    with source_lock(service.db.path) as after_release:
        assert after_release


def test_worker_runs_shared_pipeline_and_hides_unexpected_errors(service: GalleryService) -> None:
    from collections.abc import Callable

    class FixtureSource:
        def inspect(self, source_url: str, heartbeat: Callable[[], None]) -> GallerySnapshot:
            heartbeat()
            assert source_url == SOURCE
            return SNAPSHOT

    draft = service.create_draft(SOURCE, PageSelection())
    queue = AnalysisQueue(service.db, service.owner_id)
    assert AnalysisWorker(queue, FixtureSource()).run_once()
    assert service.get_draft(draft["id"])["state"] == "ready"

    class BrokenSource:
        def inspect(self, source_url: str, heartbeat: Callable[[], None]) -> GallerySnapshot:
            raise RuntimeError("a secret cookie value")

    second = service.create_draft(SOURCE, PageSelection())
    assert AnalysisWorker(queue, BrokenSource()).run_once()
    assert "secret" not in str(queue.status(second["id"]))
    assert queue.status(second["id"])["state"] == "failed"
