import asyncio
import json
from pathlib import Path

import pytest

from ehtg.bot import OwnerBot
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.settings import Settings
from ehtg.storage.database import Database
from ehtg.telegram import TelegramError


def message(update_id: int, text: str, owner: int = 123456, kind: str = "private") -> dict:
    return {
        "update_id": update_id,
        "message": {"from": {"id": owner}, "chat": {"id": owner, "type": kind}, "text": text},
    }


class Client:
    def __init__(self) -> None:
        self.calls = []
        self.failure = False

    async def call(self, method, data, sending=False):
        self.calls.append((method, data))
        if self.failure:
            raise TelegramError("DELIVERY_UNCERTAIN")
        return {"message_id": 1}


@pytest.fixture
def bot(database: Database, tmp_path: Path) -> OwnerBot:
    return OwnerBot(
        Settings(
            _env_file=None, owner_id="123456", channel_id="-100123", files_path=tmp_path / "files"
        ),
        database,
        Client(),
    )


def test_inbox_duplicate_and_owner_private_access(bot: OwnerBot) -> None:
    url = "https://e-hentai.org/g/123/abcdef0123/"
    bot.process_update(message(1, url, 999999))
    bot.process_update(message(2, url, kind="group"))
    bot.process_update(message(3, url))
    bot.process_update(message(3, url))
    with bot.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM bot_outbox").fetchone()[0] == 1
        assert connection.execute("SELECT next_offset FROM bot_state").fetchone()[0] == 4


def test_atomic_domain_change_and_inbox_rollback(
    bot: OwnerBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args, **kwargs):
        raise RuntimeError("simulated process death")

    with monkeypatch.context() as context:
        context.setattr(bot, "enqueue", crash)
        with pytest.raises(RuntimeError):
            bot.process_update(message(1, "https://e-hentai.org/g/123/abcdef0123/"))
    with bot.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM bot_inbox").fetchone()[0] == 0


def test_preview_edit_stale_callback_and_idempotent_confirm(bot: OwnerBot) -> None:
    bot.process_update(message(1, "https://e-hentai.org/g/123/abcdef0123/"))
    with bot.db.read() as connection:
        draft_id = connection.execute("SELECT id FROM drafts").fetchone()[0]
    bot.service.record_analysis(draft_id, 1, GallerySnapshot("title", ("one", "two")))
    bot.collect_notifications()
    bot.process_update(message(2, f"/pages {draft_id} 2 1 |"))
    bot.process_update(message(3, f"/publish {draft_id} 2"))
    assert bot.service.list_jobs()["items"] == []
    bot.process_update(message(4, f"/publish {draft_id} 3"))
    bot.process_update(message(4, f"/publish {draft_id} 3"))
    assert len(bot.service.list_jobs()["items"]) == 1


def test_notification_failure_never_repeats_publication(bot: OwnerBot) -> None:
    bot.enqueue("notice", "Done")
    bot.client.failure = True
    asyncio.run(bot.flush_one())
    assert not asyncio.run(bot.flush_one())
    with bot.db.read() as connection:
        assert connection.execute("SELECT state FROM bot_outbox").fetchone()[0] == "uncertain"
    assert bot.service.list_jobs()["items"] == []


def test_sent_response_payload_is_erased(bot: OwnerBot) -> None:
    bot.process_update(message(1, "/pair"))
    asyncio.run(bot.flush_one())
    with bot.db.read() as connection:
        assert (
            json.loads(connection.execute("SELECT payload_json FROM bot_outbox").fetchone()[0])
            == {}
        )
