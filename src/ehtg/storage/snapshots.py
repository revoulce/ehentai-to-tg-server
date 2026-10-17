import json
import sqlite3
from uuid import uuid4

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.domain.source import parse_gallery_url


def save_analysis(
    connection: sqlite3.Connection,
    draft: sqlite3.Row,
    snapshot: GallerySnapshot,
    *,
    max_pages: int,
    created_at: str,
) -> None:
    """The caller must validate ownership, revision and lease in this same transaction."""
    PageSelection(**json.loads(draft["selection_json"])).resolve(
        len(snapshot.page_ids), max_pages=max_pages
    )
    snapshot_id = str(uuid4())
    source_url = draft["source_url"]
    if snapshot.canonical_url:
        canonical = parse_gallery_url(snapshot.canonical_url)
        if canonical.gid != draft["gid"]:
            raise AppError("SOURCE_VERSION_CHANGED", "Ответ источника относится к другой галерее.")
        source_url = canonical.canonical
    connection.execute(
        "INSERT INTO gallery_snapshots VALUES (?,?,?,?,?)",
        (snapshot_id, source_url, snapshot.fingerprint, snapshot.json, created_at),
    )
    connection.execute(
        "UPDATE drafts SET state='ready', snapshot_id=?,source_url=?, "
        "revision=revision+1 WHERE id=?",
        (snapshot_id, source_url, draft["id"]),
    )
