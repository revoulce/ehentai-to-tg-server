import json
import sqlite3
from uuid import uuid4

from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot


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
    connection.execute(
        "INSERT INTO gallery_snapshots VALUES (?,?,?,?,?)",
        (snapshot_id, draft["source_url"], snapshot.fingerprint, snapshot.json, created_at),
    )
    connection.execute(
        "UPDATE drafts SET state='ready', snapshot_id=?, revision=revision+1 WHERE id=?",
        (snapshot_id, draft["id"]),
    )
