import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ehtg.analysis import AnalysisQueue
from ehtg.domain.selection import PageSelection
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source import child
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.errors import SourceError
from ehtg.source.session import SessionStore
from ehtg.worker import AnalysisWorker

pytest.importorskip("gallery_dl")
SOURCE = "https://e-hentai.org/g/123/abcdef0123/"


def test_real_isolated_probe_has_pinned_version(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        owner_id="123456",
        channel_id="-1001234567890",
        source_session_path=tmp_path / "missing.json",
    )
    assert GalleryDLSource(settings).probe()["version"] == "1.32.17"


def test_analysis_without_session_never_accesses_network(tmp_path: Path) -> None:
    class ForbiddenProcess:
        def run(self, *args, **kwargs):
            pytest.fail("No source process should be started without a session")

    settings = Settings(
        _env_file=None,
        owner_id="123456",
        channel_id="-1001234567890",
        source_enabled=True,
        source_session_path=tmp_path / "missing.json",
    )
    with pytest.raises(SourceError) as error:
        GalleryDLSource(settings, process=ForbiddenProcess()).inspect(SOURCE, lambda: None)
    assert error.value.code == "SOURCE_AUTH_REQUIRED"


def test_session_protocol_parser_worker_and_confirmation_together(
    service: GalleryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (Path(__file__).parent / "fixtures" / "gallery.html").read_text(encoding="utf-8")
    called = []

    class FixtureTransport:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            called.append(path)
            if path == "/home.php":
                return (
                    "You are currently at <strong>1</strong> "
                    "towards a limit of <strong>5000</strong>"
                )
            if path.endswith("?p=1"):
                return '<a href="/s/cccccccccc/123-3">3</a>'
            return html

    class FixtureProcess:
        def run(self, payload, heartbeat):
            heartbeat()
            output = io.BytesIO()
            with monkeypatch.context() as context:
                context.setattr(child, "_limits", lambda _: None)
                context.setattr(child, "SourceTransport", FixtureTransport)
                context.setattr(
                    "sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
                )
                context.setattr("sys.stdout", SimpleNamespace(buffer=output))
                child.main()
            result = json.loads(output.getvalue())
            assert result["ok"], result
            return result

    settings = Settings(
        _env_file=None,
        owner_id=service.owner_id,
        channel_id=service.channel_id,
        database_path=service.db.path,
        source_enabled=True,
        source_session_path=tmp_path / "secrets" / "session.json",
    )
    adapter = GalleryDLSource(settings, process=FixtureProcess())
    monkeypatch.setattr(adapter, "_cache_cover", lambda *args: None)
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_member_id\t123\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_pass_hash\tsynthetic-hash\n",
        encoding="utf-8",
    )
    SessionStore(settings.source_session_path).import_cookies(cookie_path, adapter.verify)
    draft = service.create_draft(SOURCE, PageSelection(exclude="2"))
    assert AnalysisWorker(AnalysisQueue(service.db, service.owner_id), adapter).run_once()
    ready = service.get_draft(draft["id"])
    assert ready["state"] == "ready"
    assert ready["selected_pages"] == [1, 3]
    assert ready["snapshot"]["authors"] == ["Alice"]
    assert ready["snapshot"]["uploader"] == "Uploader only"
    receipt = service.publish(draft["id"], ready["revision"], "full-pipeline")
    pages = service.get_job(receipt["job_id"])["pages"]
    assert [page["source_page_index"] for page in pages] == [1, 3]
    assert "synthetic-hash" not in json.dumps(ready)
    assert called == ["/home.php", "/home.php", "/g/123/abcdef0123/", "/g/123/abcdef0123/?p=1"]
