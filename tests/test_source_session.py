import json
import os
from pathlib import Path

import pytest

from ehtg.source.errors import SourceError
from ehtg.source.session import SessionCookie, SessionStore


def cookie_file(path: Path, *, expiry: int = 0) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f".e-hentai.org\tTRUE\t/\tTRUE\t{expiry}\tipb_member_id\t123\n"
        f".e-hentai.org\tTRUE\t/\tTRUE\t{expiry}\tipb_pass_hash\tsynthetic-hash\n"
        ".evil.test\tTRUE\t/\tFALSE\t0\tipb_pass_hash\tunrelated\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\ttracking\tnot-required\n",
        encoding="utf-8",
    )
    return path


def test_import_filters_domains_and_names_and_uses_private_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "secrets" / "session.json")
    verified = []
    store.import_cookies(
        cookie_file(tmp_path / "input.txt"), lambda cookies: verified.append(cookies)
    )
    assert len(verified) == 1
    assert {cookie.name for cookie in store.load()} == {"ipb_member_id", "ipb_pass_hash"}
    assert "unrelated" not in store.path.read_text()
    assert "not-required" not in store.path.read_text()
    assert "synthetic-hash" not in repr(store.load())
    assert "synthetic-hash" not in str(store.info())
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_failed_online_verification_preserves_existing_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "secrets" / "session.json")
    jar = cookie_file(tmp_path / "input.txt")
    store.import_cookies(jar, lambda _: None)
    old = store.path.read_bytes()

    def invalid(_: tuple[SessionCookie, ...]) -> None:
        raise SourceError("SOURCE_AUTH_REQUIRED")

    with pytest.raises(SourceError):
        store.import_cookies(jar, invalid)
    assert store.path.read_bytes() == old
    assert list(store.path.parent.glob(".session-*")) == []


def test_expired_cookies_never_trigger_online_check(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json", clock=lambda: 1000)
    called = []
    with pytest.raises(SourceError):
        store.import_cookies(
            cookie_file(tmp_path / "input.txt", expiry=999), lambda _: called.append(1)
        )
    assert not called
    assert not store.path.exists()


def test_untrusted_cookie_file_and_stored_session_fail_safely(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    jar = tmp_path / "input.txt"
    jar.write_text("this is not a cookie jar and contains a secret", encoding="utf-8")
    with pytest.raises(SourceError) as error:
        store.import_cookies(jar, lambda _: None)
    assert "secret" not in str(error.value)
    store.path.write_text(json.dumps({"format": 1, "cookies": "not-an-array"}), encoding="utf-8")
    with pytest.raises(SourceError):
        store.load()
    assert store.info()["state"] == "missing_or_expired"


@pytest.mark.parametrize("value", ["x\r\nInjected: true", "x; other=secret", "", '"value"'])
def test_cookie_header_injection_is_rejected(value: str) -> None:
    with pytest.raises(SourceError):
        SessionCookie("ipb_pass_hash", value)


def test_malformed_cookie_line_never_leaks_through_warning(tmp_path: Path) -> None:
    import warnings

    source = tmp_path / "malformed.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n"
        "e-hentai.org\tTRUE\t/\tFALSE\t0\tipb_pass_hash\tprivate-cookie-value\n",
        encoding="utf-8",
    )
    with warnings.catch_warnings(record=True) as captured, pytest.raises(SourceError):
        warnings.simplefilter("always")
        SessionStore(tmp_path / "session.json").import_cookies(source, lambda _: None)
    assert not captured


def test_invalid_stored_cookie_requires_session_replacement(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    store.path.write_text(
        json.dumps(
            {
                "format": 1,
                "cookies": [
                    {"name": "ipb_member_id", "value": "123"},
                    {"name": "ipb_pass_hash", "value": "value; invalid"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SourceError) as error:
        store.load()
    assert error.value.code == "SOURCE_AUTH_REQUIRED"
