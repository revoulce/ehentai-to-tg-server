import io
import socket

import pytest

from ehtg.source.errors import SourceError
from ehtg.source.session import SessionCookie
from ehtg.source.transport import (
    HOST,
    PinnedHTTPSConnection,
    SourceGate,
    SourceTransport,
    public_addresses,
    verify_authenticated_home,
)
from ehtg.storage.database import Database


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.0.1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ],
)
def test_private_or_special_dns_answers_are_rejected(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(SourceError) as error:
        public_addresses(HOST)
    assert error.value.code == "SOURCE_NETWORK_POLICY"


def test_pinned_connection_keeps_tls_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    connected = []
    names = []

    class RawSocket:
        def getpeername(self):
            return ("93.184.216.34", 443)

        def close(self):
            pass

    raw = RawSocket()

    class TLSContext:
        def wrap_socket(self, value, *, server_hostname):
            assert value is raw
            names.append(server_hostname)
            return raw

    monkeypatch.setattr("ehtg.source.transport.public_addresses", lambda _: ("93.184.216.34",))
    monkeypatch.setattr(
        "ehtg.source.transport.socket.create_connection",
        lambda address, timeout: connected.append(address) or raw,
    )
    monkeypatch.setattr("ehtg.source.transport.ssl.create_default_context", lambda: TLSContext())
    connection = PinnedHTTPSConnection(HOST)
    connection.connect()
    assert connected == [("93.184.216.34", 443)]
    assert names == [HOST]


def test_source_gate_persists_interval_and_pause(database: Database) -> None:
    clock = [1000.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    gate = SourceGate(database, 3, clock=lambda: clock[0], sleep=sleep)
    gate.wait()
    SourceGate(Database(database.path), 3, clock=lambda: clock[0], sleep=sleep).wait()
    assert sleeps == [3]
    with database.transaction() as connection:
        connection.execute("UPDATE source_control SET paused=1,reason_code='SOURCE_AUTH_REQUIRED'")
    with pytest.raises(SourceError):
        gate.wait()
    gate.wait(allow_paused=True)


@pytest.mark.parametrize(
    ("status", "headers", "body", "code"),
    [
        (302, {"Location": "http://127.0.0.1/secret"}, b"", "SOURCE_NETWORK_POLICY"),
        (403, {}, b"", "SOURCE_AUTH_REQUIRED"),
        (429, {}, b"", "SOURCE_TEMPORARILY_BLOCKED"),
        (404, {}, b"", "GALLERY_UNAVAILABLE"),
        (503, {}, b"", "SOURCE_NETWORK_UNAVAILABLE"),
        (200, {"Content-Type": "image/jpeg"}, b"data", "EXTRACTOR_INCOMPATIBLE"),
        (200, {"Content-Encoding": "gzip"}, b"data", "EXTRACTOR_INCOMPATIBLE"),
        (200, {"Content-Length": "1000000"}, b"", "RESOURCE_LIMIT"),
        (200, {"Content-Length": "20"}, b"short", "SOURCE_NETWORK_UNAVAILABLE"),
        (200, {}, b"x" * 33, "RESOURCE_LIMIT"),
        (200, {}, b"temporarily banned", "SOURCE_TEMPORARILY_BLOCKED"),
        (200, {}, b"Image limit exceeded", "SOURCE_QUOTA_EXCEEDED"),
    ],
)
def test_http_errors_and_redirects_are_classified(
    status,
    headers,
    body,
    code,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self):
            self.status = status
            self.body = io.BytesIO(body)

        def getheader(self, name, default=None):
            return {"Content-Type": "text/html", **headers}.get(name, default)

        def read(self, size):
            return self.body.read(size)

    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, *, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("ehtg.source.transport.PinnedHTTPSConnection", Connection)
    cookies = (SessionCookie("ipb_member_id", "123"), SessionCookie("ipb_pass_hash", "synthetic"))
    transport = SourceTransport(cookies, SourceGate(database, 3), max_bytes=32)
    with pytest.raises(SourceError) as error:
        transport.get("/home.php")
    assert error.value.code == code
    assert len(calls) == 1


def test_authenticated_home_requires_positive_evidence() -> None:
    verify_authenticated_home(
        "<p>You are currently at <strong>12</strong> towards a limit of <strong>5,000</strong>.</p>"
    )
    with pytest.raises(SourceError):
        verify_authenticated_home("<html>Login form or unknown markup</html>")
