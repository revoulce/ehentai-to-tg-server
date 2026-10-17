from dataclasses import replace

import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.domain.source import parse_source_url
from ehtg.service import GalleryService
from ehtg.source.session import SessionCookie
from ehtg.source.transport import API_HOST, SourceGate, SourceTransport
from ehtg.storage.database import Database


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


@pytest.mark.parametrize(
    "body,valid",
    [
        ({"tokenlist": [{"gid": 123, "token": "1111111111"}]}, True),
        ({"tokenlist": [{"gid": 124, "token": "1111111111"}]}, False),
        ({"tokenlist": [{"gid": 123, "error": "not found"}]}, False),
        ({"tokenlist": [{"gid": 123, "token": "https://evil.test"}]}, False),
    ],
)
def test_gtoken_uses_fixed_api_and_checks_correlated_result(
    body: dict, valid: bool, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    class Response:
        status = 200

        def getheader(self, name, default=None):
            return {"Content-Type": "application/json"}.get(name, default)

        def read(self, size):
            return json.dumps(body).encode()

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == API_HOST

        def request(self, method, path, *, body, headers):
            assert method == "POST" and path == "/api.php"
            assert "Cookie" not in headers and "Authorization" not in headers
            assert json.loads(body) == {"method": "gtoken", "pagelist": [[123, "abcdef0123", 2]]}

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("ehtg.source.transport.PinnedHTTPSConnection", Connection)
    transport = SourceTransport(
        (SessionCookie("ipb_member_id", "123"), SessionCookie("ipb_pass_hash", "fixture")),
        SourceGate(database, 1),
    )
    if valid:
        assert transport.gallery_token(123, "abcdef0123", 2) == "1111111111"
    else:
        with pytest.raises(AppError):
            transport.gallery_token(123, "abcdef0123", 2)
