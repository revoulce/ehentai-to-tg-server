from dataclasses import replace

import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.domain.source import parse_source_url
from ehtg.service import GalleryService


def test_image_link_becomes_gallery_draft_not_one_photo(service: GalleryService) -> None:
    url = "https://e-hentai.org/s/abcdef0123/123-2"
    source = parse_source_url(url)
    assert source.gid == 123 and source.page == 2 and source.canonical == url
    draft = service.create_draft(url, PageSelection())
    snapshot = replace(
        GallerySnapshot("gallery", ("one", "two", "three")),
        canonical_url="https://e-hentai.org/g/123/1111111111/",
    )
    service.record_analysis(draft["id"], 1, snapshot)
    ready = service.get_draft(draft["id"])
    assert ready["source_url"] == snapshot.canonical_url
    assert ready["selected_pages"] == [1, 2, 3]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.org/s/abcdef0123/123-2",
        "https://e-hentai.org/s/abcdef0123/0-2",
        "https://e-hentai.org/s/abcdef0123/123-0",
        "https://e-hentai.org:80/s/abcdef0123/123-2",
        "https://e-hentai.org/s/abcdef0123/123-2\r\n",
    ],
)
def test_image_input_is_strict(url: str) -> None:
    with pytest.raises(AppError):
        parse_source_url(url)
