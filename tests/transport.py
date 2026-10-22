"""In-process httpx transport. Tests never open a socket to OpenRouter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class HandlerTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.handler(request)
        if isinstance(result, Awaitable):
            return await result
        return result


def sent_to(transport: HandlerTransport, suffix: str) -> list[httpx.Request]:
    """Requests to one upstream path, in order.

    /health now asks upstream whether the configured model is live, so the
    transport log is no longer "first request is the call under test". Assert
    on the call you mean, not on its position.
    """
    return [request for request in transport.requests if str(request.url).endswith(suffix)]


def key_ok(handler: Handler) -> Handler:
    """Answer OpenRouter's credential-bound GET /key as accepted; defer the rest.

    OpenRouter's /models list is public, so /health asks /key whether the key
    works. A test about anything else should not have to care.
    """

    def wrapped(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"label": "test"}})
        return handler(request)

    return wrapped
