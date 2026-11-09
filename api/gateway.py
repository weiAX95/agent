"""Bound the public chat request before JSON parsing allocates its payload."""

import json
import logging
import uuid


MAX_CHAT_BODY_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


class ChatBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/chat":
            await self.app(scope, receive, send)
            return

        trace_id = uuid.uuid4().hex
        async def traced_send(event):
            if event["type"] == "http.response.start":
                event = {**event, "headers": [*event.get("headers", []),
                         (b"x-trace-id", trace_id.encode())]}
            await send(event)

        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if event["type"] != "http.request":
                continue
            body.extend(event.get("body", b""))
            if len(body) > MAX_CHAT_BODY_BYTES:
                logger.warning("chat_gateway trace_id=%s status=413", trace_id)
                payload = json.dumps({"detail": "聊天请求体不能超过 256 KiB"}).encode()
                await traced_send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(payload)).encode())]})
                await traced_send({"type": "http.response.body", "body": payload})
                return
            if not event.get("more_body", False):
                break

        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, traced_send)
