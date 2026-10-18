"""HaaS plugin authoring SDK.

Plugin tool modules import only this package (plus their own dependencies); they never
import the ``app`` package. Tools execute inside the ``haas.sandbox/1`` runner, one
subprocess per invocation, with secrets delivered over stdin.

Example::

    from pydantic import BaseModel
    from haas_plugin_sdk import ToolContext, ToolError, tool

    class Args(BaseModel):
        city: str

    @tool(name="get_weather", description="Current weather for a city.", secrets=("WEATHER_KEY",))
    async def get_weather(ctx: ToolContext, args: Args) -> str:
        key = ctx.get_secret("WEATHER_KEY")
        ...
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

__all__ = [
    "ABI_VERSION",
    "SPEC_ATTR",
    "ToolContext",
    "ToolError",
    "ToolSpec",
    "get_spec",
    "tool",
]

ABI_VERSION = "haas.sandbox/1"
SPEC_ATTR = "__haas_tool__"

F = TypeVar("F", bound=Callable[..., Any])


class ToolError(Exception):
    """A recoverable failure; the message is shown to the model so it can fix its call."""


@runtime_checkable
class ToolContext(Protocol):
    """What a tool may see about the invocation. Provided by the sandbox runner."""

    run_id: str
    thread_id: str
    tenant_id: str
    traceparent: str
    effect_key: str
    http_client: Any

    def get_secret(self, key: str) -> str: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: type[BaseModel]
    requires_approval: bool = False
    sandbox: bool = False
    secrets: tuple[str, ...] = field(default_factory=tuple)
    injects_context: bool = False


def _args_model(fn: Callable[..., Any]) -> tuple[type[BaseModel], bool]:
    params = list(inspect.signature(fn).parameters.values())
    if not params or len(params) > 2:
        raise TypeError(
            f"tool '{fn.__name__}' must take `(args)` or `(ctx, args)` parameters"
        )
    try:
        hints = typing.get_type_hints(fn)
    except Exception as exc:  # unresolved forward references
        raise TypeError(
            f"tool '{fn.__name__}': cannot resolve annotations: {exc}"
        ) from exc
    model = hints.get(params[-1].name)
    if not (inspect.isclass(model) and issubclass(model, BaseModel)):
        raise TypeError(
            f"tool '{fn.__name__}': the last parameter must be annotated with a "
            "pydantic BaseModel subclass"
        )
    return model, len(params) == 2


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    requires_approval: bool = False,
    sandbox: bool = False,
    secrets: tuple[str, ...] | list[str] = (),
) -> Callable[[F], F]:
    """Declares a plugin tool. The function stays directly callable for unit tests."""

    def decorator(fn: F) -> F:
        model, injects = _args_model(fn)
        desc = (description or inspect.getdoc(fn) or "").strip()
        if not desc:
            raise TypeError(f"tool '{fn.__name__}' needs a description or docstring")
        spec = ToolSpec(
            name=name or fn.__name__,
            description=desc,
            args_schema=model,
            requires_approval=requires_approval,
            sandbox=sandbox,
            secrets=tuple(secrets),
            injects_context=injects,
        )
        setattr(fn, SPEC_ATTR, spec)
        return fn

    return decorator


def get_spec(obj: Any) -> ToolSpec | None:
    spec = getattr(obj, SPEC_ATTR, None)
    return spec if isinstance(spec, ToolSpec) else None
