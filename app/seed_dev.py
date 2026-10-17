from __future__ import annotations

import argparse
import asyncio
import secrets
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg

from app.core.config import settings
from app.core.plugins import PluginRegistry
from app.core.store import DistributedStore
from app.core.tenant_policy import PluginGrant, TenantPolicyRules, policy_digest

TENANT_SLUG = "local-dev"
ADMIN_ISSUER = "urn:haas:local-dev"
ADMIN_SUBJECT = "local-admin"
API_KEY_NAME = "local-dev-test-key"
APPROVER_ISSUER = f"urn:haas:service:{TENANT_SLUG}"
APPROVER_SUBJECT = "local-approver"
APPROVER_KEY_NAME = "local-dev-approver-key"


async def seed_dev(*, rotate_key: bool = False) -> dict[str, str]:
    if settings.haas_profile != "dev":
        raise RuntimeError("Development seed data is only allowed in HAAS_PROFILE=dev.")
    database_host = urlsplit(settings.postgres_dsn).hostname
    if database_host not in {"postgres", "localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "Development seed data is restricted to a local PostgreSQL host."
        )

    plugin_registry = PluginRegistry()
    plugin_registry.discover_from_settings(settings)
    store = DistributedStore()
    store.pg_pool = await asyncpg.create_pool(
        dsn=settings.postgres_dsn, min_size=1, max_size=2
    )
    try:
        existing_tenant = await store.pool.fetchrow(
            """
            SELECT tenant_id::text AS tenant_id, status
            FROM tenants WHERE slug = $1
            """,
            TENANT_SLUG,
        )
        if existing_tenant is None:
            bootstrap = await store.bootstrap_tenant(
                TENANT_SLUG, ADMIN_ISSUER, ADMIN_SUBJECT
            )
            actor_id = bootstrap["principal_id"]
            tenant_id = bootstrap["tenant_id"]
        else:
            if existing_tenant["status"] != "active":
                raise RuntimeError(
                    "The local-dev tenant exists but is not active; refusing to alter it."
                )
            principal = await store.pool.fetchrow(
                """
                SELECT p.principal_id::text AS principal_id, p.status,
                       m.status AS membership_status
                FROM principals AS p
                JOIN memberships AS m ON m.principal_id = p.principal_id
                WHERE p.issuer = $1 AND p.subject = $2
                  AND m.tenant_id = $3 AND m.role = 'tenant_admin'
                """,
                ADMIN_ISSUER,
                ADMIN_SUBJECT,
                existing_tenant["tenant_id"],
            )
            if (
                principal is None
                or principal["status"] != "active"
                or principal["membership_status"] != "active"
            ):
                raise RuntimeError(
                    "The local-dev bootstrap administrator is missing or inactive."
                )
            actor_id = principal["principal_id"]
            tenant_id = existing_tenant["tenant_id"]

        admin_approver_status = await store.pool.fetchval(
            """
            SELECT status FROM memberships
            WHERE tenant_id = $1 AND principal_id = $2 AND role = 'approver'
            """,
            UUID(tenant_id),
            UUID(actor_id),
        )
        if admin_approver_status == "active":
            await store.revoke_tenant_membership(
                tenant_selector=TENANT_SLUG,
                principal_id=actor_id,
                role="approver",
                actor_principal_id=actor_id,
                request_id=str(uuid4()),
            )

        approver = await store.pool.fetchrow(
            """
            SELECT p.principal_id::text AS principal_id, p.status AS principal_status,
                   m.status AS membership_status
            FROM principals AS p
            JOIN memberships AS m ON m.principal_id = p.principal_id
            WHERE p.issuer = $1 AND p.subject = $2
              AND m.tenant_id = $3 AND m.role = 'approver'
            """,
            APPROVER_ISSUER,
            APPROVER_SUBJECT,
            UUID(tenant_id),
        )
        if (
            approver is not None
            and approver["principal_status"] == "active"
            and approver["membership_status"] == "active"
        ):
            approver_id = approver["principal_id"]
        else:
            membership = await store.grant_tenant_membership(
                tenant_selector=TENANT_SLUG,
                issuer=APPROVER_ISSUER,
                subject=APPROVER_SUBJECT,
                kind="service",
                role="approver",
                actor_principal_id=actor_id,
                request_id=str(uuid4()),
            )
            approver_id = membership["principal_id"]

        if await store.get_active_tenant_policy(TENANT_SLUG) is None:
            primary_model = settings.default_primary_model
            planner_model = settings.default_planner_model
            plugins = [
                PluginGrant(
                    plugin_id=plugin.id,
                    digest=plugin.digest,
                    skills=[
                        name
                        for name, skill in plugin_registry.skills.items()
                        if skill.plugin_id == plugin.id
                    ],
                )
                for plugin in plugin_registry.plugins.values()
                if plugin.id == "revenue-ops" and plugin.status == "loaded"
            ]
            rules = TenantPolicyRules(
                model_providers=["openrouter"],
                models=sorted({primary_model, planner_model}),
                default_primary_model=primary_model,
                default_planner_model=planner_model,
                plugin_grants=plugins,
                destination_hosts=["mock"],
                max_steps=settings.default_max_steps,
                max_cost_usd=settings.default_max_cost_usd,
            )
            await store.activate_tenant_policy_revision(
                tenant_selector=TENANT_SLUG,
                rules=rules.model_dump(mode="json", exclude_none=True),
                digest=policy_digest(rules),
                actor_principal_id=actor_id,
                request_id=str(uuid4()),
            )

        active_keys = [
            key
            for key in await store.list_tenant_api_keys(TENANT_SLUG)
            if key["name"] in (API_KEY_NAME, APPROVER_KEY_NAME)
            and key["revoked_at"] is None
        ]
        if active_keys and not rotate_key:
            raise RuntimeError(
                "A local-dev test key already exists. Use `haas dev seed --rotate-key` "
                "to revoke it and issue a replacement."
            )
        for key in active_keys:
            await store.revoke_tenant_api_key(
                tenant_selector=TENANT_SLUG,
                key_id=key["key_id"],
                actor_principal_id=actor_id,
                request_id=str(uuid4()),
            )

        credentials: dict[str, str] = {}
        for principal_id, name, result_name in (
            (actor_id, API_KEY_NAME, "admin_credential"),
            (approver_id, APPROVER_KEY_NAME, "approver_credential"),
        ):
            credential = "hk_test_" + secrets.token_urlsafe(32)
            await store.create_tenant_api_key(
                tenant_selector=TENANT_SLUG,
                principal_id=principal_id,
                credential=credential,
                name=name,
                expires_in_days=365,
                actor_principal_id=actor_id,
                request_id=str(uuid4()),
            )
            credentials[result_name] = credential
        return {
            "tenant": TENANT_SLUG,
            "principal_id": actor_id,
            **credentials,
        }
    finally:
        await store.pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed_dev")
    parser.add_argument(
        "--rotate-key",
        action="store_true",
        help="Revoke the existing local-dev key before issuing a replacement.",
    )
    args = parser.parse_args(argv)
    result: dict[str, Any] = asyncio.run(seed_dev(rotate_key=args.rotate_key))
    print(f"Seeded local tenant {result['tenant']}.")
    print(f"One-time tenant-admin/submitter API key: {result['admin_credential']}")
    print(f"One-time separate approver API key: {result['approver_credential']}")
    print("Keep both in process memory; neither is stored in a profile file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
