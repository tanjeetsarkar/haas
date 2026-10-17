from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def problem_response(
    status: int,
    detail: str,
    *,
    instance: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


async def http_exception_problem(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
    return problem_response(exc.status_code, detail, instance=request.url.path)


async def validation_problem(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {
            "loc": error.get("loc", []),
            "msg": error.get("msg", "Invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return problem_response(
        422, "Request validation failed.", instance=request.url.path, errors=errors
    )


async def unexpected_problem(request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("haas.api").error(
        "unhandled API exception",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return problem_response(
        500, "An unexpected server error occurred.", instance=request.url.path
    )


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (
                value
                for key, value in scope.get("headers", [])
                if key.lower() == b"content-length"
            ),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._too_large(scope, send)
                    return
            except ValueError:
                pass

        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await self._too_large(scope, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _too_large(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            {
                "type": "about:blank",
                "title": "Content Too Large",
                "status": 413,
                "detail": f"Request body exceeds {self.max_bytes} bytes.",
                "instance": scope.get("path", ""),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
