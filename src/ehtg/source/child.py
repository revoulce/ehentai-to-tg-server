import json
import os
import signal
import sys
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ehtg.domain.errors import AppError
from ehtg.domain.source import GalleryURL, parse_source_url
from ehtg.source.errors import SourceError
from ehtg.source.parser import inspect_gallery, probe
from ehtg.source.session import SessionCookie
from ehtg.source.transport import SourceGate, SourceTransport, verify_authenticated_home
from ehtg.storage.database import Database


class ImagePage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and values.get("id") == "img":
            self.url = values.get("src") or ""


def _limits(request: dict[str, Any]) -> None:
    if sys.platform == "linux":
        import ctypes
        import resource

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise SourceError("RESOURCE_LIMIT")
        if os.getppid() != request["parent_pid"]:
            raise SourceError("RESOURCE_LIMIT")
        memory = int(request.get("memory_mb", 256)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        file_limit = max(10_000_000, int(request.get("max_media_bytes", 10_000_000)))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))


def main() -> None:
    try:
        request_bytes = sys.stdin.buffer.read(65537)
        if len(request_bytes) > 65536:
            raise SourceError("RESOURCE_LIMIT")
        request = json.loads(request_bytes)
        _limits(request)
        operation = request["operation"]
        if operation == "probe":
            result: dict[str, Any] = {"probe": probe()}
        elif operation == "cover":
            from ehtg.media import prepare
            from ehtg.source.media_transport import download

            destination = Path(request["destination"])
            original = destination.with_suffix(".source")
            try:
                download(
                    request["url"],
                    original,
                    SourceGate(Database(Path(request["database_path"])), request["interval"]),
                    max_bytes=2_000_000,
                    timeout=request["request_timeout"],
                    min_free_bytes=request["min_free_bytes"],
                )
                result = prepare(original, destination, max_pixels=4_000_000, first_frame=True)
            finally:
                original.unlink(missing_ok=True)
        elif operation == "prepare":
            from ehtg.media import prepare

            result = prepare(
                Path(request["source_path"]),
                Path(request["photo_path"]),
                max_pixels=request["max_pixels"],
                first_frame=request["first_frame"],
            )
        elif operation in {"inspect", "verify", "download"}:
            probe()
            cookies = tuple(SessionCookie(**item) for item in request["cookies"])
            gate = SourceGate(Database(Path(request["database_path"])), request["interval"])
            transport = SourceTransport(
                cookies,
                gate,
                timeout=request["request_timeout"],
                max_bytes=request["max_html_bytes"],
                allow_paused=operation == "verify",
            )
            verify_authenticated_home(transport.get("/home.php"))
            canonical_url = ""
            if operation == "inspect":
                source = parse_source_url(request["source_url"])
                if source.page is not None:
                    canonical_url = GalleryURL(
                        source.gid, transport.gallery_token(source.gid, source.token, source.page)
                    ).canonical
                    request["source_url"] = canonical_url
            if operation == "download":
                from ehtg.source.media_transport import download

                for attempt in range(2):
                    page = ImagePage()
                    page.feed(transport.get(urlsplit(request["page_url"]).path))
                    try:
                        download(
                            page.url,
                            Path(request["destination"]),
                            SourceGate(
                                Database(Path(request["database_path"])),
                                request.get("media_interval", request["interval"]),
                            ),
                            max_bytes=request["max_media_bytes"],
                            timeout=request["request_timeout"],
                            min_free_bytes=request["min_free_bytes"],
                        )
                        break
                    except AppError as error:
                        if error.code != "MEDIA_URL_EXPIRED" or attempt == 1:
                            raise
                result = {}
            else:
                result = (
                    {}
                    if operation == "verify"
                    else {
                        "snapshot": asdict(
                            replace(
                                inspect_gallery(
                                    request["source_url"],
                                    transport.get,
                                    max_pages=request["max_pages"],
                                    max_index_pages=request["max_index_pages"],
                                ),
                                canonical_url=canonical_url,
                            ),
                        )
                    }
                )
        else:
            raise SourceError("EXTRACTOR_INCOMPATIBLE")
        response = {"protocol": 1, "ok": True, **result}
    except AppError as error:
        response = {"protocol": 1, "ok": False, "code": error.code}
    except MemoryError:
        response = {"protocol": 1, "ok": False, "code": "RESOURCE_LIMIT"}
    except Exception:
        response = {"protocol": 1, "ok": False, "code": "EXTRACTOR_INCOMPATIBLE"}
    output = json.dumps(response, ensure_ascii=True).encode()
    if len(output) > 2097152:
        output = b'{"protocol":1,"ok":false,"code":"RESOURCE_LIMIT"}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
