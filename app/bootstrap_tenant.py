from __future__ import annotations

import asyncio
import re
import asyncpg

from app.core.config import settings
from app.core.store import DistributedStore

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


async def bootstrap() -> dict[str, str]:
    slug = settings.bootstrap_tenant_slug
    issuer = settings.bootstrap_admin_issuer
    subject = settings.bootstrap_admin_subject
    if not slug or not issuer or not subject:
        raise ValueError(
            "Set HAAS_BOOTSTRAP_TENANT_SLUG, HAAS_BOOTSTRAP_ADMIN_ISSUER, "
            "and HAAS_BOOTSTRAP_ADMIN_SUBJECT before bootstrapping."
        )
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "HAAS_BOOTSTRAP_TENANT_SLUG must be a lowercase URL-safe slug."
        )
    if not settings.oidc_issuer or issuer != settings.oidc_issuer:
        raise ValueError(
            "Bootstrap administrator issuer must exactly match HAAS_OIDC_ISSUER."
        )
    if settings.haas_profile == "prod" and not issuer.startswith("https://"):
        raise ValueError("HAAS_BOOTSTRAP_ADMIN_ISSUER must use HTTPS.")

    store = DistributedStore()
    if settings.haas_profile == "prod" and not settings.migration_postgres_dsn:
        raise ValueError(
            "Production bootstrap requires a separate MIGRATION_POSTGRES_DSN."
        )
    store.pg_pool = await asyncpg.create_pool(
        settings.migration_postgres_dsn or settings.postgres_dsn,
        min_size=1,
        max_size=2,
    )
    try:
        return await store.bootstrap_tenant(slug, issuer, subject)
    finally:
        await store.close()


def main() -> int:
    result = asyncio.run(bootstrap())
    print(
        f"Bootstrapped tenant {result['tenant_slug']} "
        f"({result['tenant_id']}) with administrator principal {result['principal_id']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
