"""{{title}} tools. Loaded by `PluginRegistry.discover_all` via `register(registry)`.

Rules for files in plugins/<plugin>/tools/:
* Self-contained: files are loaded standalone, so sibling modules cannot be imported.
  (Files starting with `_` are skipped entirely.)
* Declare `ctx: RunContext` to get the run context; omit it if you do not need it.
* Use `async def` for network I/O and use `ctx.http_client`. Plain `def` handlers run in a thread.
* Raise `ToolRemediationError("what went wrong and how to fix the call")` for expected failures:
  the message goes back to the model so it can self-correct.
* Secrets: `ctx.get_secret("NAME")` works only if NAME is in `allowed_secrets` of an *active* skill.
* The tool is only offered to the model after a skill lists it in `required_tools`.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.core.context import (
    RunContext,
    ToolRemediationError,
    validate_safe_outbound_url,
)


class {{class_name}}Args(BaseModel):
    # Descriptions are part of the prompt the model sees: be specific, give formats and examples.
    query: str = Field(..., min_length=1, description="TODO: what to look up, e.g. 'acme'.")
    limit: int = Field(10, ge=1, le=50, description="Maximum number of results.")


def register(registry) -> None:
    @registry.tool(
        name="{{tool_name}}",
        description="TODO: what it returns and when to call it. Say if it is read-only or has side effects.",
        args_schema={{class_name}}Args,
        requires_approval={{requires_approval}},  # True pauses the run for a human before it executes
        sandbox={{sandbox}},  # True routes to the read-only sandbox worker queue
    )
    async def {{tool_name}}(ctx: RunContext, args: {{class_name}}Args) -> str:
        # Example: authenticated outbound HTTP call.
        #   token = ctx.get_secret("EXAMPLE_API_TOKEN")
        url = validate_safe_outbound_url("https://api.example.com/search", resolve_dns=True)
        resp = await ctx.http_client.get(
            url,
            params={"q": args.query, "limit": args.limit},
            timeout=15.0,
            follow_redirects=False,
        )
        if resp.status_code == 404:
            raise ToolRemediationError(f"Nothing found for '{args.query}'. Try a broader query.")
        resp.raise_for_status()
        # Return compact JSON/text: tool output is truncated at MAX_TOOL_OUTPUT_CHARS (4000 by default).
        return json.dumps(resp.json())
