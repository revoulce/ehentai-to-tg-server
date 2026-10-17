import asyncio
from pathlib import Path

import httpx
import pytest

from ehtg.telegram import TelegramClient, TelegramError

TOKEN = "12345:" + "x" * 32


@pytest.mark.parametrize(
    "status,body,code",
    [
        (500, {}, "DELIVERY_UNCERTAIN"),
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 65}},
            "TELEGRAM_RATE_LIMITED",
        ),
        (400, {"ok": False, "error_code": 400, "description": TOKEN}, "TELEGRAM_REJECTED"),
        (200, {"ok": True, "result": {}}, "DELIVERY_UNCERTAIN"),
    ],
)
def test_send_failures_are_safe_and_classified(
    tmp_path: Path, status: int, body: dict, code: str
) -> None:
    async def run() -> None:
        client = TelegramClient(
            TOKEN, transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
        )
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"photo")
        with pytest.raises(TelegramError) as error:
            await client.send_part(
                "-100123",
                {"caption": "caption"},
                [path],
                {"disable_notification": True, "has_spoiler": False},
            )
        assert error.value.code == code
        assert TOKEN not in str(error.value)
        await client.close()

    asyncio.run(run())


def test_multipart_upload_and_photo_flags(tmp_path: Path) -> None:
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "message_id": i,
                        "chat": {"id": -100123},
                        "photo": [{"file_id": str(i)}],
                        "media_group_id": "album",
                    }
                    for i in (1, 2)
                ],
            },
        )

    async def run() -> None:
        client = TelegramClient(TOKEN, transport=httpx.MockTransport(transport))
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"photo")
        result = await client.send_part(
            "-100123",
            {"caption": "<raw>"},
            [path, path],
            {"disable_notification": True, "has_spoiler": True},
        )
        assert result[0]["link"] == "https://t.me/c/123/1"
        assert (
            b"attach://p0" in requests[0].content and b'"has_spoiler": true' in requests[0].content
        )
        assert b"parse_mode" not in requests[0].content
        await client.close()

    asyncio.run(run())


def test_http_client_logs_redact_token(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    async def run() -> None:
        client = TelegramClient(
            TOKEN,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"ok": True, "result": {"id": 123}})
            ),
        )
        with caplog.at_level(logging.INFO, logger="httpx"):
            await client.call("getMe")
        assert TOKEN not in caplog.text and "[redacted]" in caplog.text
        await client.close()

    asyncio.run(run())
