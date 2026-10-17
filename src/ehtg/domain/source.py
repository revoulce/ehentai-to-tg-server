import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ehtg.domain.errors import AppError

_GALLERY_PATH = re.compile(r"/g/([1-9][0-9]{0,17})/([0-9a-fA-F]{10})/?\Z")


@dataclass(frozen=True)
class GalleryURL:
    gid: int
    token: str

    @property
    def canonical(self) -> str:
        return f"https://e-hentai.org/g/{self.gid}/{self.token}/"


def parse_gallery_url(value: str) -> GalleryURL:
    """Validate before any source access; never pass an arbitrary URL to an extractor."""
    invalid = AppError("INVALID_SOURCE_URL", "Нужна ссылка галереи https://e-hentai.org/g/…/…/.")
    if not value or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise invalid
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or url.netloc.lower() not in {"e-hentai.org", "e-hentai.org:443"}
            or url.hostname != "e-hentai.org"
            or url.username is not None
            or url.password is not None
            or url.port not in {None, 443}
            or (url.scheme == "http" and url.port is not None)
        ):
            raise invalid
    except ValueError:
        raise invalid from None
    match = _GALLERY_PATH.fullmatch(url.path)
    if match is None:
        raise invalid
    return GalleryURL(int(match[1]), match[2].lower())
