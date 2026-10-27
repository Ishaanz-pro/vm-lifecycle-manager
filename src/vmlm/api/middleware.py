"""Pure-ASGI middleware: request IDs and HTTP metrics.

Written against the ASGI interface directly (not BaseHTTPMiddleware) so contextvars set here
are visible to the endpoint and to log lines emitted while it runs, and so streaming
responses are untouched.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from vmlm.observability.context import new_request_id, request_id_var
from vmlm.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("vmlm.api")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

HEADER = b"x-request-id"
MAX_ID_LEN = 64


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(HEADER, b"").decode("latin-1")
        request_id = (
            incoming[:MAX_ID_LEN] if incoming.isprintable() and incoming else new_request_id()
        )
        token = request_id_var.set(request_id)
        status_holder: dict[str, Any] = {"status": 500, "started": False}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                headers = list(message.get("headers") or [])
                headers.append((HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:  # noqa: BLE001 - last line of defence: generic 500, no details leaked
            log.exception("unhandled_exception")
            if status_holder["status"] == 500 and not status_holder.get("started"):
                body = json.dumps(
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "internal server error",
                            "details": {},
                            "request_id": request_id,
                        }
                    }
                ).encode()
                await send_wrapper(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
        finally:
            request_id_var.reset(token)
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            labels = {
                "method": scope["method"],
                "route": template,
                "status": str(status_holder["status"]),
            }
            HTTP_REQUESTS.inc(**labels)
            HTTP_LATENCY.observe(
                time.perf_counter() - started, method=scope["method"], route=template
            )
