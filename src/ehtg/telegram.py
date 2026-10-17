import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from ehtg.domain.errors import AppError


class TelegramError(AppError):
    def __init__(self, code: str, *, retry_after: int = 0) -> None:
        messages = {
            "TELEGRAM_UNAVAILABLE": "Telegram временно недоступен.",
            "TELEGRAM_RATE_LIMITED": "Telegram ограничил частоту запросов.",
            "TELEGRAM_PERMISSION_DENIED": "Проверьте токен, канал и право публикации бота.",
            "TELEGRAM_REJECTED": "Telegram отклонил запрос. Требуется исправление.",
            "DELIVERY_UNCERTAIN": "Ответ отправки неизвестен. Сверьте канал вручную.",
        }
        super().__init__(
            code,
            messages[code],
            503,
            retryable=code in {"TELEGRAM_UNAVAILABLE", "TELEGRAM_RATE_LIMITED"},
            details={"retry_after": retry_after},
        )


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", token) is None:
            raise TelegramError("TELEGRAM_PERMISSION_DENIED")
        self._url = "https://api.telegram.org/bot" + token + "/"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=15),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
        sending: bool = False,
    ) -> Any:
        if method not in {
            "getMe",
            "getChat",
            "getChatMember",
            "sendPhoto",
            "sendMediaGroup",
            "sendMessage",
            "editMessageText",
            "answerCallbackQuery",
            "getUpdates",
        }:
            raise TelegramError("TELEGRAM_REJECTED")
        try:
            response = await self._client.post(
                self._url + method,
                **({"data": data, "files": files} if files else {"json": data or {}}),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise TelegramError("TELEGRAM_UNAVAILABLE") from None
        except httpx.HTTPError:
            raise TelegramError(
                "DELIVERY_UNCERTAIN" if sending else "TELEGRAM_UNAVAILABLE"
            ) from None
        if response.status_code >= 500:
            raise TelegramError("DELIVERY_UNCERTAIN" if sending else "TELEGRAM_UNAVAILABLE")
        try:
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
                raise ValueError
            if body["ok"] and response.status_code == 200 and "result" in body:
                return body["result"]
            code = body.get("error_code", response.status_code)
            if code == 429:
                delay = body.get("parameters", {}).get("retry_after", 60)
                if type(delay) is not int or delay < 1:
                    delay = 60
                raise TelegramError("TELEGRAM_RATE_LIMITED", retry_after=delay)
            if code in {401, 403}:
                raise TelegramError("TELEGRAM_PERMISSION_DENIED")
            if code == 400:
                raise TelegramError("TELEGRAM_REJECTED")
        except (ValueError, TypeError, AttributeError):
            pass
        raise TelegramError("DELIVERY_UNCERTAIN" if sending else "TELEGRAM_UNAVAILABLE")

    async def check_channel(self, channel_id: str) -> None:
        try:
            bot = await self.call("getMe")
            chat = await self.call("getChat", {"chat_id": channel_id})
            member = await self.call("getChatMember", {"chat_id": channel_id, "user_id": bot["id"]})
            if (
                str(chat["id"]) != channel_id
                or chat["type"] != "channel"
                or not (
                    member["status"] == "creator"
                    or (member["status"] == "administrator" and member.get("can_post_messages"))
                )
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TelegramError("TELEGRAM_PERMISSION_DENIED") from None

    async def send_part(
        self, channel_id: str, part: dict[str, Any], paths: list[Path], options: dict[str, Any]
    ) -> list[dict[str, Any]]:
        with ExitStack() as stack:
            files = {
                f"p{i}": (f"{i}.jpg", stack.enter_context(path.open("rb")), "image/jpeg")
                for i, path in enumerate(paths)
            }
            data = {
                "chat_id": channel_id,
                "disable_notification": json.dumps(options["disable_notification"]),
            }
            if len(paths) == 1:
                data.update(
                    {"caption": part["caption"], "has_spoiler": json.dumps(options["has_spoiler"])}
                )
                result = await self.call(
                    "sendPhoto", data, files={"photo": files["p0"]}, sending=True
                )
                messages = [result]
            else:
                media = [
                    {
                        "type": "photo",
                        "media": f"attach://p{i}",
                        "has_spoiler": options["has_spoiler"],
                        **({"caption": part["caption"]} if i == 0 else {}),
                    }
                    for i in range(len(paths))
                ]
                data["media"] = json.dumps(media, ensure_ascii=False)
                messages = await self.call("sendMediaGroup", data, files=files, sending=True)
        try:
            if not isinstance(messages, list) or len(messages) != len(paths):
                raise ValueError
            normalized = []
            for message in messages:
                if (
                    type(message["message_id"]) is not int
                    or message["message_id"] < 1
                    or str(message["chat"]["id"]) != channel_id
                    or not message.get("photo")
                ):
                    raise ValueError
                normalized.append(
                    {
                        "message_id": message["message_id"],
                        "media_group_id": message.get("media_group_id"),
                        "file_id": message["photo"][-1]["file_id"],
                        "link": post_link(channel_id, message["message_id"]),
                    }
                )
            return normalized
        except (KeyError, TypeError, ValueError, IndexError):
            raise TelegramError("DELIVERY_UNCERTAIN") from None


def post_link(channel_id: str, message_id: int) -> str:
    if re.fullmatch(r"-100[0-9]+", channel_id) is None or message_id < 1:
        raise ValueError("Invalid channel message")
    return f"https://t.me/c/{channel_id[4:]}/{message_id}"
