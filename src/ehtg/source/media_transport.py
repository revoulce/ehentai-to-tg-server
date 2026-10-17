import http.client
import ipaddress
import os
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from ehtg.source.errors import SourceError
from ehtg.source.transport import SourceGate


def media_target(value: str) -> tuple[str, int, str]:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (
            len(value) > 4096
            or url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.fragment
            or not host
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or not (
                host == "e-hentai.org"
                or host.endswith(".hath.network")
                or host == "ehgt.org"
                or host.endswith(".ehgt.org")
            )
            or (url.port is not None and url.port != 443 and url.port < 1024)
            or url.path.rsplit("/", 1)[-1].lower() in {"509.gif", "509.jpg", "509.png"}
            or "quota" in url.path.lower()
        ):
            raise ValueError
        return host, url.port or 443, url.path + ("?" + url.query if url.query else "")
    except ValueError:
        raise SourceError("SOURCE_NETWORK_POLICY") from None


class MediaConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        try:
            addresses = tuple(
                dict.fromkeys(
                    str(row[4][0])
                    for row in socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
                )
            )
            if (
                not addresses
                or len(addresses) > 16
                or any(
                    not ipaddress.ip_address(value).is_global
                    or ipaddress.ip_address(value).is_multicast
                    or "%" in value
                    for value in addresses
                )
            ):
                raise SourceError("SOURCE_NETWORK_POLICY")
            raw = socket.create_connection((addresses[0], self.port), self.timeout)
            try:
                if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(addresses[0]):
                    raise SourceError("SOURCE_NETWORK_POLICY")
                self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
            except BaseException:
                raw.close()
                raise
        except (OSError, ValueError):
            raise SourceError("SOURCE_NETWORK_UNAVAILABLE") from None


def download(
    url: str,
    destination: Path,
    gate: SourceGate,
    *,
    max_bytes: int,
    timeout: float,
    min_free_bytes: int,
) -> None:
    import shutil

    host, port, path = media_target(url)
    if shutil.disk_usage(destination.parent).free < max_bytes + min_free_bytes:
        raise SourceError("DISK_FULL")
    gate.wait()
    connection = MediaConnection(host, port, timeout=timeout)
    temporary = destination.with_suffix(".part")
    try:
        # Media requests deliberately carry no E-Hentai cookies, proxy or API credentials.
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "image/*",
                "Accept-Encoding": "identity",
                "User-Agent": "ehentai-to-tg/1.0",
            },
        )
        response = connection.getresponse()
        if response.status in {403, 404, 410}:
            raise SourceError("MEDIA_URL_EXPIRED")
        if response.status == 429:
            raise SourceError("SOURCE_TEMPORARILY_BLOCKED")
        if response.status != 200:
            raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
        if (
            not response.getheader("Content-Type", "").lower().startswith("image/")
            or response.getheader("Content-Encoding", "identity").lower() != "identity"
        ):
            raise SourceError("MEDIA_INVALID")
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
            raise SourceError("RESOURCE_LIMIT")
        size = 0
        with temporary.open("wb") as handle:
            while chunk := response.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise SourceError("RESOURCE_LIMIT")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not size or (declared is not None and size != int(declared)):
            raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
        os.replace(temporary, destination)
    except (OSError, http.client.HTTPException):
        raise SourceError("SOURCE_NETWORK_UNAVAILABLE") from None
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)
