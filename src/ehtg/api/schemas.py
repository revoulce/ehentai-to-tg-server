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


class PublishDraft(StrictBody):
    """Only the exact draft revision in If-Match can be confirmed; no client overrides."""


class RedeemCode(StrictBody):
    code: str = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
