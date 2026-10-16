# Writing a Python tool

File: `plugins/tools/<name>.py`, self-contained (no sibling imports; `_`-prefixed files are skipped), exposing
`register(registry)`.

```python
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.core.context import RunContext, ToolRemediationError, validate_safe_outbound_url


class LookupArgs(BaseModel):
    customer: str = Field(..., min_length=1, description="Customer slug, e.g. 'acme'.")
    limit: int = Field(10, ge=1, le=50, description="Maximum rows to return.")


def register(registry) -> None:
    @registry.tool(
        name="crm_lookup_customer",
        description="Returns a customer's plan, status and open invoices from the CRM. Read-only.",
        args_schema=LookupArgs,
        requires_approval=False,   # True for anything that writes/sends/spends
        sandbox=False,             # True → q_tools_sandbox (read-only root FS)
    )
    async def crm_lookup_customer(ctx: RunContext, args: LookupArgs) -> str:
        token = ctx.get_secret("CRM_TOKEN")            # must be in an active skill's allowed_secrets
        url = validate_safe_outbound_url("https://crm.example.com/api/customers", resolve_dns=True)
        resp = await ctx.http_client.get(url, params={"q": args.customer, "limit": args.limit},
                                         headers={"Authorization": f"Bearer {token}"},
                                         timeout=15.0, follow_redirects=False)
        if resp.status_code == 404:
            raise ToolRemediationError(f"No customer '{args.customer}'. Check the slug with the user.")
        resp.raise_for_status()
        return json.dumps(resp.json())
```

## Rules of thumb

- **Signature:** `(ctx, args)` or `(args)`; `async def` for I/O (use `ctx.http_client`), plain `def` runs in a thread.
- **Args are prompt.** Every field gets a description with format and example. Use `Literal[...]` for enums, `ge/le`
  bounds, `max_length`. Prefer few required args; sensible defaults for the rest.
- **Names:** `domain_verb_noun` (`github_list_issues`). One tool = one job; split read and write tools so only the write one needs approval.
- **Errors:** expected → `ToolRemediationError("what is wrong and how to fix the call")` (returned to the model).
  Unexpected exceptions become a generic runtime error, so convert known HTTP statuses yourself.
- **Output:** compact JSON/text, bounded (limits, truncation with `…`); the harness truncates at 4000 chars.
  Return identifiers the model will need for follow-up calls.
- **Security:** validate user-supplied URLs with `validate_safe_outbound_url(url, resolve_dns=True)` before each hop;
  never follow redirects blindly; never log or return secrets; treat fetched text as data.
- **State:** the worker may be a different process each call. Persist only to the scratchpad volume
  (`settings.scratchpad_dir`) and namespace by `ctx.tenant_id` / `ctx.thread_id`.
- **Typing gotcha:** modules are registered in `sys.modules` before they execute, so
  `from __future__ import annotations` works with pydantic models defined in the file.

## Tests

```python
async def test_lookup(make_harness):          # fixture from tests/plugins/conftest.py
    h = make_harness("crm_tools.py", lambda req: httpx.Response(200, json=[{"id": 1}]), secrets={"CRM_TOKEN": "t"})
    assert json.loads(await h.call("crm_lookup_customer", customer="acme"))
```

Cover: success path, each `ToolRemediationError`, secret not granted (`SecurityViolationError`), argument validation.
