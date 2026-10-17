import json
import os
import re
import sys
import tempfile
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from uuid import uuid4

from ehtg.service import utc_now
from ehtg.source.errors import SourceError

_NAMES = frozenset({"ipb_member_id", "ipb_pass_hash"})
MAX_SESSION_BYTES = 65536


@dataclass(frozen=True)
class SessionCookie:
    name: str
    value: str = field(repr=False)
    expires: int | None = None

    def __post_init__(self) -> None:
        if (
            self.name not in _NAMES
            or re.fullmatch(r"[A-Za-z0-9._~%+=:/-]{1,4096}", self.value) is None
            or (self.expires is not None and (type(self.expires) is not int or self.expires <= 0))
        ):
            raise SourceError("INVALID_COOKIE_FILE")


def check_cookies(cookies: tuple[SessionCookie, ...], now: float) -> None:
    if len(cookies) != 2 or {cookie.name for cookie in cookies} != _NAMES:
        raise SourceError("SOURCE_AUTH_REQUIRED")
    if any(cookie.expires is not None and cookie.expires <= now for cookie in cookies):
        raise SourceError("SOURCE_AUTH_REQUIRED")


class SessionStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def import_cookies(
        self,
        source: Path,
        validate: Callable[[tuple[SessionCookie, ...]], None],
    ) -> None:
        try:
            if source.stat().st_size > MAX_SESSION_BYTES:
                raise SourceError("INVALID_COOKIE_FILE")
            jar = MozillaCookieJar(str(source))
            # cookiejar warnings can contain the malformed input line and its secrets.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, LoadError, ValueError):
            raise SourceError("INVALID_COOKIE_FILE") from None
        selected: dict[str, SessionCookie] = {}
        for item in jar:
            if item.domain not in {"e-hentai.org", ".e-hentai.org"} or item.name not in _NAMES:
                continue
            if item.path != "/":
                continue
            cookie = SessionCookie(item.name, item.value or "", item.expires or None)
            if cookie.name in selected and cookie != selected[cookie.name]:
                raise SourceError("INVALID_COOKIE_FILE")
            selected[cookie.name] = cookie
        cookies = tuple(selected[name] for name in sorted(selected))
        check_cookies(cookies, self.clock())
        validate(
            cookies
        )  # Do not replace a working session until an authenticated request succeeds.
        self._save(cookies)

    def _read(self) -> dict[str, object]:
        try:
            if self.path.is_symlink():
                raise SourceError("SOURCE_AUTH_REQUIRED")
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_SESSION_BYTES + 1)
            if len(raw) > MAX_SESSION_BYTES:
                raise SourceError("SOURCE_AUTH_REQUIRED")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("format") != 1:
                raise SourceError("SOURCE_AUTH_REQUIRED")
            return data
        except (OSError, ValueError):
            raise SourceError("SOURCE_AUTH_REQUIRED") from None

    def load(self) -> tuple[SessionCookie, ...]:
        try:
            data = self._read()
            raw = data.get("cookies")
            if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                raise SourceError("SOURCE_AUTH_REQUIRED")
            cookies = tuple(SessionCookie(**item) for item in raw)
            check_cookies(cookies, self.clock())
            return cookies
        except (SourceError, KeyError, TypeError, ValueError):
            raise SourceError("SOURCE_AUTH_REQUIRED") from None

    def info(self) -> dict[str, object]:
        try:
            data = self._read()
            self.load()
            return {
                "state": "installed",
                "generation": data["generation"],
                "verified_at": data["verified_at"],
            }
        except (SourceError, KeyError):
            return {"state": "missing_or_expired"}

    def _save(self, cookies: tuple[SessionCookie, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise SourceError("INVALID_COOKIE_FILE")
        payload = json.dumps(
            {
                "format": 1,
                "generation": str(uuid4()),
                "verified_at": utc_now(),
                "cookies": [asdict(cookie) for cookie in cookies],
            }
        ).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if sys.platform != "win32":
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
