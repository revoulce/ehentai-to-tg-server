import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from ehtg.analysis import AnalysisQueue
from ehtg.bot import OwnerBot
from ehtg.domain.selection import PageSelection
from ehtg.publisher import PublicationWorker
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source import child
from ehtg.source.adapter import GalleryDLSource
from ehtg.storage.database import Database
from ehtg.telegram import TelegramClient
from ehtg.worker import AnalysisWorker


def test_bot_source_media_journal_and_telegram_together(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (Path(__file__).parent / "fixtures/gallery.html").read_text(encoding="utf-8")

    class FixtureTransport:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/home.php":
                return "You are currently at 1 towards a limit of 5000"
            if path.startswith("/s/"):
                return '<img id="img" src="https://images.hath.network/fixture.jpg">'
            if path.endswith("?p=1"):
                return '<a href="/s/cccccccccc/123-3">3</a>'
            return html

    def download(url, destination, *args, **kwargs):
        Image.new("RGB", (300, 100), "red").save(destination, format="PNG")

    class FixtureProcess:
        def run(self, payload, heartbeat):
            heartbeat()
            output = io.BytesIO()
            with monkeypatch.context() as context:
                context.setattr(child, "_limits", lambda _: None)
                context.setattr(child, "SourceTransport", FixtureTransport)
                context.setattr("ehtg.source.media_transport.download", download)
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
        owner_id="123456",
        channel_id="-100123",
        source_enabled=True,
        database_path=database.path,
        source_session_path=tmp_path / "session.json",
        files_path=tmp_path / "files",
        min_free_bytes=0,
    )
    source = GalleryDLSource(settings, process=FixtureProcess())
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_member_id\t123\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_pass_hash\tfixture\n",
        encoding="utf-8",
    )
    source.sessions.import_cookies(cookies, source.verify)
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        results = {
            "getMe": {"id": 987},
            "getChat": {"id": -100123, "type": "channel"},
            "getChatMember": {"status": "administrator", "can_post_messages": True},
        }
        result = results.get(
            method,
            [
                {
                    "message_id": index,
                    "chat": {"id": -100123},
                    "photo": [{"file_id": f"file-{index}"}],
                    "media_group_id": "album",
                }
                for index in (11, 12)
            ],
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    async def run() -> None:
        telegram = TelegramClient("987:" + "x" * 32, transport=httpx.MockTransport(transport))
        bot = OwnerBot(settings, database, telegram)
        bot.process_update(
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 123456},
                    "chat": {"id": 123456, "type": "private"},
                    "text": "https://e-hentai.org/g/123/abcdef0123/",
                },
            }
        )
        assert AnalysisWorker(AnalysisQueue(database, "123456"), source).run_once()
        with database.read() as connection:
            draft_id = connection.execute("SELECT id FROM drafts").fetchone()[0]
        service = bot.service
        draft = service.edit_selection(
            draft_id,
            2,
            PageSelection(exclude="2"),
        )
        receipt = service.publish(draft_id, draft["revision"], "integration")
        assert await PublicationWorker(service, settings, source, telegram).run_once()
        job = service.get_job(receipt["job_id"])
        assert job["state"] == "completed" and job["published_pages"] == 2
        assert [p["source_page_index"] for p in job["pages"]] == [1, 3]
        assert calls.count("sendMediaGroup") == 1
        with database.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 4
        restarted = GalleryService(database, "123456", "-100123")
        assert restarted.publish(draft_id, draft["revision"], "integration") == receipt
        assert source.cover_path(draft["snapshot"]["cover_url"]).is_file()
        await telegram.close()

    asyncio.run(run())
