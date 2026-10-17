import io
import socket
from pathlib import Path

import pytest

from ehtg.domain.errors import AppError
from ehtg.source.media_transport import MediaConnection, download
from ehtg.source.transport import SourceGate
from ehtg.storage.database import Database


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_media_dns_cannot_reach_private_network(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(AppError) as error:
        MediaConnection("fixture.hath.network", 443).connect()
    assert error.value.code == "SOURCE_NETWORK_POLICY"


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (302, {"Location": "https://evil.test/"}, b"", "SOURCE_NETWORK_UNAVAILABLE"),
        (403, {}, b"", "MEDIA_URL_EXPIRED"),
        (200, {"Content-Type": "text/html"}, b"login", "MEDIA_INVALID"),
        (
            200,
            {"Content-Type": "image/png", "Content-Length": "20"},
            b"partial",
            "SOURCE_NETWORK_UNAVAILABLE",
        ),
        (200, {"Content-Type": "image/png"}, b"x" * 40, "RESOURCE_LIMIT"),
    ],
)
def test_streaming_failures_leave_no_ready_file(
    status: int,
    headers: dict,
    body: bytes,
    expected: str,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self):
            self.status, self.stream = status, io.BytesIO(body)

        def getheader(self, name, default=None):
            return headers.get(name, default)

        def read(self, size):
            return self.stream.read(size)

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, headers):
            assert "Cookie" not in headers and "Authorization" not in headers

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("ehtg.source.media_transport.MediaConnection", Connection)
    destination = tmp_path / "image.source"
    with pytest.raises(AppError) as error:
        download(
            "https://fixture.hath.network/image",
            destination,
            SourceGate(database, 1),
            max_bytes=32,
            timeout=1,
            min_free_bytes=0,
        )
    assert error.value.code == expected
    assert not destination.exists() and not destination.with_suffix(".part").exists()
