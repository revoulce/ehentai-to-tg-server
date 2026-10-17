from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBoundary:
    """Bound streamed bodies as well as Content-Length; never echo request contents."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def safe_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *(
                        header
                        for header in message["headers"]
                        if header[0].lower() not in {b"x-request-id", b"cache-control"}
                    ),
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                response = JSONResponse(
                    {
                        "code": "RESOURCE_LIMIT",
                        "message": "Тело запроса слишком велико.",
                        "retryable": False,
                        "request_id": request_id,
                        "details": {},
                    },
                    status_code=413,
                )
                await response(scope, receive, safe_send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        consumed = False

        async def buffered_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, safe_send)
