from pathlib import Path

import pytest

from ehtg.source.errors import SourceError
from ehtg.source.parser import GalleryIndex, inspect_gallery, probe

pytest.importorskip("gallery_dl")
SOURCE = "https://e-hentai.org/g/123/abcdef0123/"


def gallery_html(pages: int = 3) -> str:
    fixture = Path(__file__).parent / "fixtures" / "gallery.html"
    return fixture.read_text(encoding="utf-8").replace("3 pages", f"{pages} pages")


def test_pinned_distribution_contains_expected_extractor() -> None:
    result = probe()
    assert result["version"] == "1.32.17"
    assert len(result["extractor_sha256"]) == 64


def test_inspection_uses_actual_page_links_and_preserves_metadata() -> None:
    called = []

    def fetch(path):
        called.append(path)
        return gallery_html() if "?" not in path else '<a href="/s/cccccccccc/123-3">3</a>'

    snapshot = inspect_gallery(SOURCE, fetch)
    assert len(snapshot.page_ids) == 3
    assert snapshot.page_ids[-1].endswith("/123-3")
    assert snapshot.title == "Synthetic & gallery"
    assert snapshot.authors == ("Alice",)
    assert snapshot.groups == ("Team",)
    assert snapshot.uploader == "Uploader only"
    assert '"filecount":"3"' in snapshot.raw_metadata_json
    assert called == ["/g/123/abcdef0123/", "/g/123/abcdef0123/?p=1"]


def test_missing_page_is_not_a_success() -> None:
    with pytest.raises(SourceError) as error:
        inspect_gallery(
            SOURCE, lambda path: gallery_html() if "?" not in path else "<html>no pages</html>"
        )
    assert error.value.code == "EXTRACTOR_INCOMPATIBLE"


def test_source_change_and_unbounded_pagination_are_rejected() -> None:
    index = GalleryIndex(SOURCE)
    index.feed('<a href="/s/aaaaaaaaaa/123-1">1</a>')
    with pytest.raises(SourceError) as error:
        index.feed('<a href="/s/bbbbbbbbbb/123-1">changed</a>')
    assert error.value.code == "SOURCE_VERSION_CHANGED"
    with pytest.raises(SourceError):
        inspect_gallery(SOURCE, lambda _: gallery_html().replace("?p=1", "?p=9999"))


def test_no_author_is_not_replaced_with_uploader() -> None:
    page = gallery_html(2).replace("/tag/artist:Alice", "/tag/language:english")
    page = page.replace(f'<a href="{SOURCE}?p=1">next</a>', "")
    assert inspect_gallery(SOURCE, lambda _: page).authors == ()


def test_wrong_extractor_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ehtg.source.parser.importlib.metadata.version", lambda _: "999.0")
    with pytest.raises(SourceError) as error:
        probe()
    assert error.value.code == "EXTRACTOR_INCOMPATIBLE"
