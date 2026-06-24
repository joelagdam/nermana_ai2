"""Compatibility module.

Termux/mobile builds use ``python -m nermana serve`` which starts the
standard-library web server. This file intentionally avoids importing FastAPI
so old launch commands do not crash with ``ModuleNotFoundError``.
"""

from __future__ import annotations


async def app(scope, receive, send):
    if scope.get("type") != "http":
        return
    body = b"Nermana mobile server uses: python -m nermana serve\n"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
