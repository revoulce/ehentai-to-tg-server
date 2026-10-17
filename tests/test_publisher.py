import asyncio
from pathlib import Path
from typing import Any

import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.publisher import JobControl, PublicationWorker
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source.locking import source_lock
from ehtg.telegram import TelegramError


class Source:
    def __init__(self, count: int) -> None:
        self.snapshot = GallerySnapshot("Fixture", tuple(str(i) for i in range(count)))

    def inspect(self, url: str, heartbeat: Any) -> GallerySnapshot:
        heartbeat()
        return self.snapshot


class Artifacts:
    def __init__(self, path: Path) -> None:
        self.prepared: list[int] = []
        self.file = path / "photo"
        self.file.write_bytes(b"fixture")

    def prepare_page(
        self, source: Any, job: str, page: int, identity: str, heartbeat: Any, **kwargs: Any
    ) -> None:
        heartbeat()
        self.prepared.append(page)

    def path(self, *args: Any) -> Path:
        return self.file

    def valid(self, *args: Any) -> bool:
        return True

    def cleanup(self) -> int:
        return 0


class Telegram:
    def __init__(self, artifacts: Artifacts, count: int) -> None:
        self.parts: list[int] = []
        self.artifacts, self.count = artifacts, count
        self.fail_part: int | None = None
        self.code = "DELIVERY_UNCERTAIN"

    async def check_channel(self, channel: str) -> None:
        pass

    async def send_part(self, channel: str, part: dict[str, Any], paths: Any, options: Any) -> Any:
        assert len(set(self.artifacts.prepared)) == self.count
        if part["index"] == self.fail_part:
            raise TelegramError(self.code, retry_after=61)
        self.parts.append(part["index"])
        return [{"message_id": part["index"] * 10 + i} for i in range(len(paths))]


def fixture(service: GalleryService, tmp_path: Path, count: int = 21) -> tuple[Any, ...]:
    source = Source(count)
    draft = service.create_draft("https://e-hentai.org/g/123/abcdef0123/", PageSelection())
    service.record_analysis(draft["id"], 1, source.snapshot)
    job_id = service.publish(draft["id"], 2, "confirm")["job_id"]
    artifacts = Artifacts(tmp_path)
    telegram = Telegram(artifacts, count)
    settings = Settings(
        _env_file=None,
        owner_id=service.owner_id,
        channel_id=service.channel_id,
        files_path=tmp_path / "files",
    )
    worker = PublicationWorker(
        service, settings, source, telegram, artifacts=artifacts, clock=lambda: 1000
    )
    return job_id, worker, telegram, source


@pytest.mark.parametrize("count", [1, 2, 10, 11, 20, 21])
def test_complete_only_after_all_pages_and_parts(
    service: GalleryService, tmp_path: Path, count: int
) -> None:
    job_id, worker, telegram, _ = fixture(service, tmp_path, count)
    assert asyncio.run(worker.run_once())
    job = service.get_job(job_id)
    assert job["state"] == "completed" and job["published_pages"] == count
    assert len(telegram.parts) == (count + 9) // 10
    assert all(part["state"] == "sent" for part in job["parts"])


def test_uncertain_send_stops_without_replaying_then_manual_resume(
    service: GalleryService, tmp_path: Path
) -> None:
    job_id, worker, telegram, _ = fixture(service, tmp_path)
    telegram.fail_part = 2
    asyncio.run(worker.run_once())
    job = service.get_job(job_id)
    assert job["state"] == "needs_review" and job["published_pages"] == 10
    assert not asyncio.run(worker.run_once())
    with pytest.raises(AppError):
        JobControl(service).action(job_id, job["revision"], "retry")
    JobControl(service).review(job_id, job["revision"], "sent", evidence="https://t.me/c/123/99")
    telegram.fail_part = None
    asyncio.run(worker.run_once())
    assert telegram.parts == [1, 3]
    assert service.get_job(job_id)["state"] == "completed"


def test_rate_limit_does_not_replay_confirmed_parts(
    service: GalleryService, tmp_path: Path
) -> None:
    job_id, worker, telegram, _ = fixture(service, tmp_path)
    telegram.fail_part, telegram.code = 2, "TELEGRAM_RATE_LIMITED"
    asyncio.run(worker.run_once())
    assert service.get_job(job_id)["retry_at"] == 1061
    assert not asyncio.run(worker.run_once())
    worker.clock = lambda: 2000
    telegram.fail_part = None
    asyncio.run(worker.run_once())
    assert telegram.parts == [1, 2, 3]


def test_live_sender_lock_and_source_change(service: GalleryService, tmp_path: Path) -> None:
    job_id, worker, telegram, source = fixture(service, tmp_path)
    with source_lock(service.db.path.with_name("publisher-" + service.db.path.name)):
        assert not asyncio.run(worker.run_once())
    source.snapshot = GallerySnapshot("Changed", ("other",))
    asyncio.run(worker.run_once())
    assert service.get_job(job_id)["error_code"] == "SOURCE_VERSION_CHANGED"
    assert telegram.parts == []


def test_crash_with_sending_part_requires_review(service: GalleryService, tmp_path: Path) -> None:
    job_id, worker, telegram, _ = fixture(service, tmp_path)
    with service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO publication_parts(job_id,part_index,state) VALUES (?,1,'sending')",
            (job_id,),
        )
    assert not asyncio.run(worker.run_once())
    assert service.get_job(job_id)["error_code"] == "DELIVERY_UNCERTAIN"
    assert telegram.parts == []
