"""Health-only HTTP application for the Atlas Explorer service."""

from fastapi import FastAPI

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report process liveness without probing external dependencies."""
    return {"status": "ok"}
