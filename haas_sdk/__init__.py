"""Python SDK for the Harness-as-a-Service (HaaS) ``/v1`` API.

The SDK depends only on ``httpx``; it never imports the server package.
"""

from haas_sdk import webhooks
from haas_sdk._version import __version__
from haas_sdk.async_client import AsyncHaas
from haas_sdk.client import Haas
from haas_sdk.errors import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    HaasConnectionError,
    HaasError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    StreamClosedError,
    ValidationError,
    WebhookVerificationError,
)
from haas_sdk.models import (
    AgentRevision,
    ApprovalBatch,
    Artifact,
    Page,
    Run,
    RunEvent,
    Thread,
    WebhookEvent,
)

__all__ = [
    "APIError",
    "AgentRevision",
    "ApprovalBatch",
    "Artifact",
    "AsyncHaas",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "Haas",
    "HaasConnectionError",
    "HaasError",
    "NotFoundError",
    "Page",
    "PermissionDeniedError",
    "RateLimitError",
    "Run",
    "RunEvent",
    "ServerError",
    "StreamClosedError",
    "Thread",
    "ValidationError",
    "WebhookEvent",
    "WebhookVerificationError",
    "__version__",
    "webhooks",
]
