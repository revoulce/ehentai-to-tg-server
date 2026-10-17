import hashlib
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from ehtg.domain.errors import AppError
from ehtg.settings import Settings
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.process import IsolatedProcess
from ehtg.storage.database import Database


class ArtifactStore:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database
        self.root = settings.files_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, job_id: str, page: int, kind: str) -> Path:
        if str(UUID(job_id)) != job_id or not 1 <= page <= 2000 or kind not in {"source", "photo"}:
            raise AppError("INVALID_ARTIFACT", "Недопустимый артефакт.")
        folder = self.root / job_id
        folder.mkdir(exist_ok=True, mode=0o700)
        if folder.resolve().parent != self.root:
            raise AppError("INVALID_ARTIFACT", "Каталог задания повреждён.")
        path = folder / f"{page:04d}.{kind}"
        if path.is_symlink():
            raise AppError("INVALID_ARTIFACT", "Файл задания повреждён.")
        return path

    def valid(self, job_id: str, page: int, kind: str) -> bool:
        path = self.path(job_id, page, kind)
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT sha256,size FROM artifacts WHERE job_id=? AND page_index=? AND kind=?",
                (job_id, page, kind),
            ).fetchone()
        if row is None or not path.is_file() or path.stat().st_size != row["size"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
        return bool(digest.hexdigest() == row["sha256"])

    def save(self, job_id: str, page: int, metadata: dict[str, Any]) -> None:
        with self.db.transaction() as connection:
            for kind in ("source", "photo"):
                item = metadata[kind]
                connection.execute(
                    "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        page,
                        kind,
                        item["sha256"],
                        item["size"],
                        item["width"],
                        item["height"],
                        json.dumps(item["transforms"]),
                    ),
                )
            connection.execute(
                "UPDATE job_pages SET state='prepared' WHERE job_id=? AND source_page_index=?",
                (job_id, page),
            )

    def prepare_page(
        self,
        source: GalleryDLSource,
        job_id: str,
        page: int,
        identity: str,
        heartbeat: Callable[[], None],
        *,
        first_frame: bool = False,
    ) -> None:
        if self.valid(job_id, page, "source") and self.valid(job_id, page, "photo"):
            return
        original = self.path(job_id, page, "source")
        photo = self.path(job_id, page, "photo")
        # Unrecorded files are untrusted after a crash and must be fetched again.
        if not self.valid(job_id, page, "source"):
            with self.db.read() as connection:
                used = connection.execute(
                    "SELECT COALESCE(SUM(size),0) FROM artifacts WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            if used + self.settings.media_max_bytes + 9_500_000 > self.settings.job_max_bytes:
                raise AppError("RESOURCE_LIMIT", "Достигнут предел файлов задания.")
            source.media_process.run(
                {
                    "operation": "download",
                    "page_url": identity,
                    "destination": str(original),
                    "max_media_bytes": self.settings.media_max_bytes,
                    "min_free_bytes": self.settings.min_free_bytes,
                    "media_interval": self.settings.media_request_interval,
                    **source._payload(source.sessions.load()),
                },
                heartbeat,
            )
        result = IsolatedProcess(timeout=self.settings.media_timeout).run(
            {
                "operation": "prepare",
                "source_path": str(original),
                "photo_path": str(photo),
                "max_pixels": self.settings.media_max_pixels,
                "first_frame": first_frame,
                "memory_mb": self.settings.source_memory_mb,
            },
            heartbeat,
        )
        self.save(job_id, page, result)
        self.manifest(job_id)

    def manifest(self, job_id: str) -> None:
        with self.db.read() as connection:
            items = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM artifacts WHERE job_id=? ORDER BY page_index,kind", (job_id,)
                )
            ]
        folder = self.path(job_id, 1, "source").parent
        temporary = folder / "manifest.tmp"
        temporary.write_text(
            json.dumps(
                {"version": 1, "job_id": job_id, "artifacts": items}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        temporary.replace(folder / "manifest.json")

    def cleanup(self) -> int:
        removed = 0
        with self.db.read() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE state='completed' AND completed_at<=?",
                    (time.time() - self.settings.file_retention_seconds,),
                )
            ]
        for job_id in ids:
            folder = self.root / str(UUID(job_id))
            if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == self.root:
                shutil.rmtree(folder)
                removed += 1
        covers = self.root / "covers"
        if covers.is_dir() and not covers.is_symlink():
            cutoff = time.time() - self.settings.file_retention_seconds
            for path in covers.glob("*.jpg"):
                if not path.is_symlink() and path.stat().st_mtime <= cutoff:
                    path.unlink(missing_ok=True)
        return removed
