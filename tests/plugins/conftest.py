from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.core.context import RunContext
from app.core.plugins import PluginRegistry

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def load_tools(filename: str) -> PluginRegistry:
    """Loads a plugins/tools/*.py file exactly like PluginRegistry.discover_all does."""
    path = PLUGINS / "tools" / filename
    spec = importlib.util.spec_from_file_location(f"haas_plugin_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    reg = PluginRegistry()
    mod.register(reg)
    return reg


class Harness:
    """Calls registered tools with a RunContext backed by an httpx.MockTransport."""

    def __init__(
        self,
        reg: PluginRegistry,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        secrets: dict[str, str] | None = None,
        tenant: str = "t1",
        thread: str = "thd_1",
    ) -> None:
        self.reg = reg
        self.requests: list[httpx.Request] = []

        def recording(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            assert handler is not None, f"unexpected HTTP call {req.method} {req.url}"
            return handler(req)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        secrets = secrets or {}
        self.ctx = RunContext(
            run_id="run_t",
            thread_id=thread,
            tenant_id=tenant,
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            allowed_secrets=set(secrets),
            _scoped_secrets=secrets,
            http_client=self.client,
        )

    async def call(self, tool: str, **kwargs: Any) -> str:
        t = self.reg.tools[tool]
        args = t.args_schema.model_validate(kwargs)
        out = t.handler(self.ctx, args) if t.injects_context else t.handler(args)
        if hasattr(out, "__await__"):
            out = await out
        return out

    async def aclose(self) -> None:
        await self.client.aclose()


@pytest.fixture
def make_harness():
    made: list[Harness] = []

    def factory(filename: str, handler=None, **kw) -> Harness:
        h = Harness(load_tools(filename), handler, **kw)
        made.append(h)
        return h

    yield factory
