import hashlib
import importlib
import importlib.metadata
import importlib.resources
import re
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ehtg.domain.snapshot import GallerySnapshot, canonical_json
from ehtg.domain.source import parse_gallery_url
from ehtg.source.errors import SourceError

PINNED_VERSION = "1.32.17"
SOURCE_COMMIT = "61971e01d394d35c81a86d4d0d03717c78ae53d5"
EXTRACTOR_SHA256 = "15d9f56b3676ee9519d99ad8c3508f1e9487a2cfb244cb80561ca843140ae758"


def probe() -> dict[str, str]:
    try:
        version = importlib.metadata.version("gallery-dl")
        code = (
            importlib.resources.files("gallery_dl.extractor").joinpath("exhentai.py").read_bytes()
        )
        digest = hashlib.sha256(code).hexdigest()
        if version != PINNED_VERSION or digest != EXTRACTOR_SHA256:
            raise SourceError("EXTRACTOR_INCOMPATIBLE")
        module = importlib.import_module("gallery_dl.extractor.exhentai")
        if module.ExhentaiGalleryExtractor.from_url("https://e-hentai.org/g/1/abcdef0123/") is None:
            raise SourceError("EXTRACTOR_INCOMPATIBLE")
        return {"version": version, "extractor_sha256": digest, "source_commit": SOURCE_COMMIT}
    except (ImportError, OSError, AttributeError):
        raise SourceError("EXTRACTOR_INCOMPATIBLE") from None


class GalleryIndex(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = parse_gallery_url(source_url)
        self.pages: dict[int, str] = {}
        self.last_index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urlsplit(urljoin(self.source.canonical, href))
        if url.scheme != "https" or url.netloc != "e-hentai.org":
            return
        match = re.fullmatch(r"/s/([0-9a-f]{10})/([1-9][0-9]*)-([1-9][0-9]*)", url.path)
        if match and int(match[2]) == self.source.gid:
            index = int(match[3])
            identity = f"https://e-hentai.org{url.path}"
            if index in self.pages and self.pages[index] != identity:
                raise SourceError("SOURCE_VERSION_CHANGED")
            self.pages[index] = identity
        if url.path == urlsplit(self.source.canonical).path:
            query = parse_qs(url.query, keep_blank_values=True)
            if set(query) == {"p"} and len(query["p"]) == 1 and query["p"][0].isdigit():
                self.last_index = max(self.last_index, int(query["p"][0]))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise SourceError("EXTRACTOR_INCOMPATIBLE")


def inspect_gallery(
    source_url: str,
    fetch: Callable[[str], str],
    *,
    max_pages: int = 2000,
    max_index_pages: int = 200,
) -> GallerySnapshot:
    probe()
    source = parse_gallery_url(source_url)
    path = urlsplit(source.canonical).path
    first_page = fetch(path)
    try:
        module = importlib.import_module("gallery_dl.extractor.exhentai")
        # Only this pure parser method is invoked. We never initialize or iterate the extractor,
        # create its requests session, resolve media, follow children, or run postprocessors.
        metadata = module.ExhentaiGalleryExtractor.from_url(source.canonical).metadata_from_page(
            first_page
        )
        page_count = int(metadata["filecount"])
        if not 1 <= page_count <= max_pages:
            raise SourceError("RESOURCE_LIMIT")
        index = GalleryIndex(source.canonical)
        index.feed(first_page)
        pending = 1
        while pending <= index.last_index:
            if index.last_index >= max_index_pages or index.last_index >= page_count:
                raise SourceError("RESOURCE_LIMIT")
            page = fetch(f"{path}?p={pending}")
            index.feed(page)
            pending += 1
        if set(index.pages) != set(range(1, page_count + 1)):
            raise SourceError("EXTRACTOR_INCOMPATIBLE")
        tags = tuple(
            dict.fromkeys(
                (tag.partition(":")[0], tag.partition(":")[2]) if ":" in tag else ("misc", tag)
                for tag in metadata.get("tags", [])
            )
        )
        return GallerySnapshot(
            title=metadata.get("title") or metadata.get("title_jpn") or "",
            original_title=metadata.get("title_jpn", ""),
            category=metadata.get("eh_category", ""),
            page_ids=tuple(index.pages[number] for number in range(1, page_count + 1)),
            tags=tags,
            uploader=metadata.get("uploader", ""),
            language=metadata.get("language", ""),
            authors=tuple(name for namespace, name in tags if namespace == "artist"),
            groups=tuple(name for namespace, name in tags if namespace == "group"),
            cover_url=metadata.get("thumb", ""),
            parent_url=metadata.get("parent", ""),
            raw_metadata_json=canonical_json(_jsonable(metadata)),
        )
    except SourceError:
        raise
    except Exception:
        raise SourceError("EXTRACTOR_INCOMPATIBLE") from None
