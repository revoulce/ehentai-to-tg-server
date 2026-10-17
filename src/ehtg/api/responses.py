from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

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
    uploader: str = ""
    language: str = ""
    authors: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    cover_url: str = ""
    parent_url: str = ""
    raw_metadata_json: str = "{}"


class AnalysisResponse(BaseModel):
    state: Literal["pending", "running", "retry_wait", "paused", "succeeded", "failed", "cancelled"]
    attempts: int
    retry_at: datetime | None
    error_code: str | None
    error_message: str | None
    retryable: bool


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
    analysis: AnalysisResponse


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
