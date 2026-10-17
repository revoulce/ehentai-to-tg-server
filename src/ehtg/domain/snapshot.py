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

    def __post_init__(self) -> None:
        if (
            not self.title.strip()
            or len(self.title) > 2048
            or not self.page_ids
            or len(self.page_ids) > 2000
            or any(not identity or len(identity) > 512 for identity in self.page_ids)
        ):
            raise AppError("EXTRACTOR_INCOMPATIBLE", "Источник вернул неполный снимок галереи.")

    @property
    def json(self) -> str:
        return canonical_json(asdict(self))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.json.encode()).hexdigest()
