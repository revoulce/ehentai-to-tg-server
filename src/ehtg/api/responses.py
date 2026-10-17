from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from ehtg.api.schemas import SelectionBody

JobState = Literal[
    "queued",
    "downloading",
    "preparing",
    "publishing",
    "retry_wait",
    "paused",
    "needs_review",
    "completed",
    "failed",
    "cancelled",
]


class SnapshotResponse(BaseModel):
    title: str
    page_ids: list[str]
    original_title: str
    category: str
    tags: list[tuple[str, str]]
    fingerprint: str


class DraftResponse(BaseModel):
    id: UUID
    owner_id: str
    gid: int
    source_url: str
    state: Literal["analyzing", "ready", "error", "confirmed"]
    revision: int
    snapshot_id: UUID | None
    created_at: datetime
    page_selection: SelectionBody
    snapshot: SnapshotResponse | None
    selected_pages: list[int]


class PublicationReceipt(BaseModel):
    job_id: UUID
    state: Literal["queued"]
    created_at: datetime


class PageResponse(BaseModel):
    source_page_index: int
    page_identity: str
    state: Literal["pending", "verified", "prepared"]


class JobResponse(BaseModel):
    sequence: int
    id: UUID
    draft_id: UUID
    owner_id: str
    gid: int
    channel_id: str
    snapshot_id: UUID
    page_selection: SelectionBody
    state: JobState
    published_pages: int
    published_parts: int
    created_at: datetime
    pages: list[PageResponse]


class JobSummary(BaseModel):
    sequence: int
    id: UUID
    state: JobState
    created_at: datetime
    published_pages: int
    published_parts: int


class JobList(BaseModel):
    items: list[JobSummary]
    next_cursor: int | None


class DeviceResponse(BaseModel):
    id: UUID
    name: str
    origin: str
    created_at: datetime
    revoked_at: datetime | None


class DeviceList(BaseModel):
    items: list[DeviceResponse]


class PairingResponse(BaseModel):
    device_id: UUID
    token: str
    token_type: Literal["Bearer"]


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: UUID
    details: dict[str, object]
