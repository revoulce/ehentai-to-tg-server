from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SelectionBody(StrictBody):
    include: str = Field(default="", max_length=4096)
    exclude: str = Field(default="", max_length=4096)


class CreateDraft(StrictBody):
    source_url: str = Field(max_length=2048)
    page_selection: SelectionBody = Field(default_factory=SelectionBody)


class EditDraft(StrictBody):
    page_selection: SelectionBody
    options: "OptionsBody | None" = None


class OptionsBody(StrictBody):
    title: str | None = Field(default=None, max_length=2048)
    caption: str | None = Field(default=None, max_length=4096)
    removed_tags: list[str] = Field(default_factory=list, max_length=2000)
    disable_notification: bool = True
    has_spoiler: bool = False


class CreateBatch(StrictBody):
    source_urls: list[str] = Field(min_length=1, max_length=50)


class EditBatch(StrictBody):
    draft_ids: list[str] = Field(min_length=1, max_length=50)


class DraftRevision(StrictBody):
    id: str
    revision: int = Field(ge=1)


class PublishBatch(StrictBody):
    items: list[DraftRevision] = Field(min_length=1, max_length=50)


class ReviewJob(StrictBody):
    decision: Literal["sent", "not_sent", "cancel", "first_frame", "exclude"]
    evidence: str = Field(default="", max_length=2048)
    page: int | None = Field(default=None, ge=1, le=2000)


class PublishDraft(StrictBody):
    """Only the exact draft revision in If-Match can be confirmed; no client overrides."""


class RedeemCode(StrictBody):
    code: str = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
