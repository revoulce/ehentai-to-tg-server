import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from collections.abc import Callable
from html import unescape

from ehtg.source.errors import SourceError
from ehtg.source.session import SessionCookie, check_cookies
from ehtg.storage.database import Database

HOST = "e-hentai.org"
API_HOST = "api.e-hentai.org"


def public_addresses(host: str) -> tuple[str, ...]:
    if host not in {HOST, API_HOST}:
        raise SourceError("SOURCE_NETWORK_POLICY")
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise SourceError("SOURCE_NETWORK_UNAVAILABLE") from None
    addresses = tuple(dict.fromkeys(str(result[4][0]) for result in results))
    if not addresses or len(addresses) > 16:
        raise SourceError("SOURCE_NETWORK_POLICY")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise SourceError("SOURCE_NETWORK_POLICY") from None
        if not address.is_global or address.is_multicast or address.is_unspecified or "%" in value:
            raise SourceError("SOURCE_NETWORK_POLICY")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, timeout: float = 20) -> None:
        if host not in {HOST, API_HOST}:
            raise SourceError("SOURCE_NETWORK_POLICY")
        self.tls_context = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout, context=self.tls_context)

    def connect(self) -> None:
        addresses = public_addresses(self.host)
        raw = socket.create_connection((addresses[0], 443), self.timeout)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(addresses[0]):
                raise SourceError("SOURCE_NETWORK_POLICY")
            # Pin the connection to the checked IP while validating TLS for the original host.
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SourceGate:
    def __init__(
        self,
        database: Database,
        interval: float,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = database
        self.interval = interval
        self.clock = clock
        self.sleep = sleep

    def wait(self, *, allow_paused: bool = False) -> None:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM source_control").fetchone()
            if row["paused"] and not allow_paused:
                raise SourceError(row["reason_code"] or "SOURCE_TEMPORARILY_BLOCKED")
            at = max(self.clock(), row["next_request_at"])
            connection.execute("UPDATE source_control SET next_request_at=?", (at + self.interval,))
        delay = at - self.clock()
        if delay > 0:
            self.sleep(delay)
        with self.db.read() as connection:
            row = connection.execute("SELECT paused,reason_code FROM source_control").fetchone()
        if row["paused"] and not allow_paused:
            raise SourceError(row["reason_code"] or "SOURCE_TEMPORARILY_BLOCKED")


class SourceTransport:
    def __init__(
        self,
        cookies: tuple[SessionCookie, ...],
        gate: SourceGate,
        *,
        timeout: float = 20,
        max_bytes: int = 2097152,
        allow_paused: bool = False,
    ) -> None:
        check_cookies(cookies, time.time())
        self.cookies = cookies
        self.gate = gate
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allow_paused = allow_paused

    def get(self, path: str) -> str:
        check_cookies(self.cookies, time.time())
        if (
            re.fullmatch(
                r"/(?:home\.php|g/[1-9][0-9]{0,17}/[a-f0-9]{10}/(?:\?p=[0-9]{1,4})?"
                r"|s/[a-f0-9]{10}/[1-9][0-9]{0,17}-[1-9][0-9]{0,8})",
                path,
            )
            is None
        ):
            raise SourceError("SOURCE_NETWORK_POLICY")
        self.gate.wait(allow_paused=self.allow_paused)
        connection = PinnedHTTPSConnection(HOST, timeout=self.timeout)
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Cookie": "; ".join(
                        [*(f"{item.name}={item.value}" for item in self.cookies), "nw=1"]
                    ),
                    "User-Agent": "ehentai-to-tg/0.2 source-inspector",
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                # No redirect is followed, so credentials never leave the exact HTTPS origin.
                raise SourceError(
                    "SOURCE_AUTH_REQUIRED"
                    if "login" in response.getheader("Location", "").lower()
                    else "SOURCE_NETWORK_POLICY"
                )
            if response.status in {401, 403}:
                raise SourceError("SOURCE_AUTH_REQUIRED")
            if response.status == 429:
                raise SourceError("SOURCE_TEMPORARILY_BLOCKED")
            if response.status == 404:
                raise SourceError("GALLERY_UNAVAILABLE")
            if response.status != 200:
                raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
            if (
                not response.getheader("Content-Type", "").lower().startswith("text/html")
                or response.getheader("Content-Encoding", "identity").lower() != "identity"
            ):
                raise SourceError("EXTRACTOR_INCOMPATIBLE")
            expected_length: int | None = None
            if (declared_length := response.getheader("Content-Length")) is not None:
                if not declared_length.isdigit():
                    raise SourceError("EXTRACTOR_INCOMPATIBLE")
                expected_length = int(declared_length)
                if expected_length > self.max_bytes:
                    raise SourceError("RESOURCE_LIMIT")
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise SourceError("RESOURCE_LIMIT")
            if expected_length is not None and len(body) != expected_length:
                raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
            text = body.decode("utf-8")
        except (OSError, http.client.HTTPException):
            raise SourceError("SOURCE_NETWORK_UNAVAILABLE") from None
        except UnicodeError:
            raise SourceError("EXTRACTOR_INCOMPATIBLE") from None
        finally:
            connection.close()
        lower = text.lower()
        if "temporarily banned" in lower or "temporarily blocked" in lower:
            raise SourceError("SOURCE_TEMPORARILY_BLOCKED")
        if "image limit exceeded" in lower:
            raise SourceError("SOURCE_QUOTA_EXCEEDED")
        if "this page requires you to log on" in lower or not text.strip():
            raise SourceError("SOURCE_AUTH_REQUIRED")
        return text

    def gallery_token(self, gid: int, token: str, page: int) -> str:
        self.gate.wait()
        connection = PinnedHTTPSConnection(API_HOST, timeout=self.timeout)
        try:
            payload = json.dumps({"method": "gtoken", "pagelist": [[gid, token, page]]}).encode()
            connection.request(
                "POST",
                "/api.php",
                body=payload,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status == 429:
                raise SourceError("SOURCE_TEMPORARILY_BLOCKED")
            if 300 <= response.status < 400:
                raise SourceError("SOURCE_NETWORK_POLICY")
            if response.status != 200:
                raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
            if (
                not response.getheader("Content-Type", "").lower().startswith("application/json")
                or response.getheader("Content-Encoding", "identity") != "identity"
            ):
                raise SourceError("EXTRACTOR_INCOMPATIBLE")
            raw = response.read(16385)
            if len(raw) > 16384:
                raise SourceError("RESOURCE_LIMIT")
            body = json.loads(raw)
            item = body["tokenlist"][0]
            if int(item["gid"]) != gid or re.fullmatch(r"[0-9a-f]{10}", item["token"]) is None:
                raise ValueError
            return str(item["token"])
        except (OSError, http.client.HTTPException):
            raise SourceError("SOURCE_NETWORK_UNAVAILABLE") from None
        except (KeyError, TypeError, ValueError, IndexError):
            raise SourceError("GALLERY_UNAVAILABLE") from None
        finally:
            connection.close()


def verify_authenticated_home(page: str) -> None:
    plain = unescape(re.sub(r"<[^>]+>", " ", page))
    plain = " ".join(plain.split())
    if (
        re.search(r"You are currently at\s+[0-9,]+\s+towards a limit of\s+[0-9,]+", plain, re.I)
        is None
    ):
        raise SourceError("SOURCE_AUTH_REQUIRED")
