import hashlib
import json
from dataclasses import asdict, dataclass

from ehtg.domain.errors import AppError


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class GallerySnapshot:
    """Trusted adapter output. Page identity is independent of expiring download URLs."""

    title: str
    page_ids: tuple[str, ...]
    original_title: str = ""
    category: str = ""
    tags: tuple[tuple[str, str], ...] = ()
    uploader: str = ""
    language: str = ""
    authors: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    cover_url: str = ""
    parent_url: str = ""
    raw_metadata_json: str = "{}"

    def __post_init__(self) -> None:
        if (
            not self.title.strip()
            or len(self.title) > 2048
            or not self.page_ids
            or len(self.page_ids) > 2000
            or any(not identity or len(identity) > 512 for identity in self.page_ids)
            or len(self.original_title) > 2048
            or len(self.cover_url) > 2048
            or len(self.parent_url) > 2048
            or len(self.uploader) > 512
            or len(self.language) > 128
            or len(self.category) > 128
            or len(self.authors) > 2000
            or len(self.groups) > 2000
            or any(len(name) > 512 for name in (*self.authors, *self.groups))
            or len(self.raw_metadata_json) > 262144
            or len(self.tags) > 2000
            or any(len(namespace) > 128 or len(name) > 512 for namespace, name in self.tags)
        ):
            raise AppError("EXTRACTOR_INCOMPATIBLE", "Источник вернул неполный снимок галереи.")
        try:
            if not isinstance(json.loads(self.raw_metadata_json), dict):
                raise ValueError
        except ValueError:
            raise AppError("EXTRACTOR_INCOMPATIBLE", "Метаданные источника повреждены.") from None

    @property
    def json(self) -> str:
        return canonical_json(asdict(self))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.json.encode()).hexdigest()
