import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.source import parse_gallery_url


@pytest.mark.parametrize(
    "value",
    [
        "http://e-hentai.org/g/123/ABCDEF0123/?p=2#top",
        "https://e-hentai.org:443/g/123/abcdef0123",
        "https://E-HENTAI.ORG/g/123/abcdef0123/",
    ],
)
def test_url_is_canonicalized(value: str) -> None:
    assert parse_gallery_url(value).canonical == "https://e-hentai.org/g/123/abcdef0123/"


@pytest.mark.parametrize(
    "value",
    [
        "https://e-hentai.org.evil.test/g/123/abcdef0123/",
        "https://evil.test@e-hentai.org/g/123/abcdef0123/",
        "https://e-hentai.org@127.0.0.1/g/123/abcdef0123/",
        "https://e-hentai.org:8443/g/123/abcdef0123/",
        "https://e-hentai.org:/g/123/abcdef0123/",
        "http://e-hentai.org:443/g/123/abcdef0123/",
        "https://e-hentai.org./g/123/abcdef0123/",
        "https://e-hentai.org/g/123/abcdef0123/\n",
        "https://e-hentai.org/g/123/abcdef0123/%2e%2e/",
        "https://e-hentai.org/g/0/abcdef0123/",
        "https://e-hentai.org/s/abcdef0123/123-1",
        "ehentai:https://e-hentai.org/g/123/abcdef0123/",
        "file:///etc/passwd",
        "https://127.0.0.1/g/123/abcdef0123/",
    ],
)
def test_unsafe_and_unsupported_urls_are_rejected(value: str) -> None:
    with pytest.raises(AppError, match="ссылка галереи"):
        parse_gallery_url(value)


def test_selection_orders_deduplicates_and_excludes() -> None:
    assert PageSelection("5,1-4,2-6", "3,5-6").resolve(10) == (1, 2, 4)
    assert PageSelection(exclude="2-3").resolve(4) == (1, 4)


@pytest.mark.parametrize("expression", ["0", "-1", "3-1", "1,,2", "1.5", "1-", "1;2"])
def test_invalid_selection_syntax(expression: str) -> None:
    with pytest.raises(AppError):
        PageSelection(expression)


@pytest.mark.parametrize(
    ("selection", "code"),
    [
        (PageSelection("1-11"), "PAGE_OUT_OF_RANGE"),
        (PageSelection(exclude="11"), "PAGE_OUT_OF_RANGE"),
        (PageSelection(exclude="1-10"), "EMPTY_SELECTION"),
    ],
)
def test_selection_rejects_invalid_result(selection: PageSelection, code: str) -> None:
    with pytest.raises(AppError) as error:
        selection.resolve(10)
    assert error.value.code == code


def test_huge_range_is_checked_before_expansion() -> None:
    with pytest.raises(AppError) as error:
        PageSelection("1-999999999").resolve(10)
    assert error.value.code == "PAGE_OUT_OF_RANGE"
