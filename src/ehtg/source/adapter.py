from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from ehtg.domain.snapshot import GallerySnapshot
from ehtg.domain.source import parse_source_url
from ehtg.settings import Settings
from ehtg.source.errors import SourceError
from ehtg.source.process import IsolatedProcess
from ehtg.source.session import SessionCookie, SessionStore


class GalleryDLSource:
    def __init__(self, settings: Settings, *, process: IsolatedProcess | None = None) -> None:
        self.settings = settings
        self.sessions = SessionStore(settings.source_session_path)
        self.process = process or IsolatedProcess(timeout=settings.analysis_timeout)

    def probe(self) -> dict[str, Any]:
        return dict(
            self.process.run(
                {"operation": "probe", "memory_mb": self.settings.source_memory_mb}, lambda: None
            )["probe"]
        )

    def _payload(self, cookies: tuple[SessionCookie, ...]) -> dict[str, Any]:
        return {
            "cookies": [asdict(cookie) for cookie in cookies],
            "database_path": str(self.settings.database_path.resolve()),
            "interval": self.settings.source_request_interval,
            "request_timeout": self.settings.source_request_timeout,
            "max_html_bytes": self.settings.source_max_html_bytes,
            "max_pages": self.settings.max_pages,
            "max_index_pages": self.settings.source_max_index_pages,
            "memory_mb": self.settings.source_memory_mb,
        }

    def verify(self, cookies: tuple[SessionCookie, ...]) -> None:
        self.process.run({"operation": "verify", **self._payload(cookies)}, lambda: None)

    def inspect(self, source_url: str, heartbeat: Callable[[], None]) -> GallerySnapshot:
        source = parse_source_url(source_url)
        if not self.settings.source_enabled:
            raise SourceError("SOURCE_AUTH_REQUIRED")
        cookies = self.sessions.load()
        result = self.process.run(
            {"operation": "inspect", "source_url": source.canonical, **self._payload(cookies)},
            heartbeat,
        )
        try:
            snapshot = result["snapshot"]
            for field in ("page_ids", "tags", "authors", "groups"):
                snapshot[field] = tuple(
                    tuple(tag) if isinstance(tag, list) else tag for tag in snapshot[field]
                )
            return GallerySnapshot(**snapshot)
        except (KeyError, TypeError, ValueError):
            raise SourceError("EXTRACTOR_INCOMPATIBLE") from None
