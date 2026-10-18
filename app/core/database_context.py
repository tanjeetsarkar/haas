from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

tenant_context: ContextVar[str] = ContextVar("haas_database_tenant", default="")


class DatabaseTenantMiddleware:
    def __init__(self, app: ASGIApp, *, allow_legacy_dev: bool = False) -> None:
        self.app = app
        self.allow_legacy_dev = allow_legacy_dev

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = [
            value.decode("latin-1")
            for key, value in scope["headers"]
            if key.lower() == b"x-tenant-id"
        ]
        selected = values[0].strip() if len(values) == 1 else ""
        if not values and self.allow_legacy_dev:
            selected = "default-tenant"
        token = tenant_context.set(selected)
        try:
            await self.app(scope, receive, send)
        finally:
            tenant_context.reset(token)
