"""`preview.artifacts`: `/v1/artifacts*` (server routes differ from SDK 0.1, §9.8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, BinaryIO

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.models import Page
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class Artifact:
    """An artifact record (`GET /v1/artifacts/{id}`)."""

    artifact_id: str
    run_id: str
    name: str
    content_type: str
    size_bytes: int
    sha256: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=str(data.get("artifact_id") or data.get("id") or ""),
            run_id=str(data.get("run_id") or ""),
            name=str(data.get("name") or ""),
            content_type=str(data.get("content_type") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            sha256=str(data.get("sha256") or ""),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ArtifactUpload:
    """Response of `POST /v1/artifacts` (multipart)."""

    artifact_id: str
    size_bytes: int
    sha256: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactUpload:
        return cls(
            artifact_id=str(data.get("artifact_id") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            sha256=str(data.get("sha256") or ""),
            raw=data,
        )


def _artifact_page(response: Any) -> Page[Artifact]:
    body = _dict(response)
    items = body.get("items") or []
    return Page(
        items=tuple(Artifact.from_dict(i) for i in items if isinstance(i, dict)),
        next_cursor=body.get("next_cursor"),
    )


class Artifacts(_PreviewResource):
    """Artifact CRUD against the server's actual routes (§9.8)."""

    def list(
        self, run_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Page[Artifact]:
        params = clean({"run_id": run_id, "limit": limit, "cursor": cursor})
        return self._run(
            specs.Op(
                Call("GET", "/v1/artifacts", params=params, idempotent=True),
                _artifact_page,
            )
        )

    def get(self, artifact_id: str) -> Artifact:
        return self._run(
            specs.Op(
                Call("GET", f"/v1/artifacts/{seg(artifact_id)}", idempotent=True),
                lambda r: Artifact.from_dict(_dict(r)),
            )
        )

    def download(self, artifact_id: str) -> bytes:
        """Download the artifact bytes (`GET .../download`)."""
        response = self._client._send(
            Call("GET", f"/v1/artifacts/{seg(artifact_id)}/download", idempotent=True)
        )
        return response.content

    def upload(
        self,
        run_id: str,
        content: bytes | BinaryIO,
        *,
        name: str,
        content_type: str = "application/octet-stream",
        source: str = "api",
    ) -> ArtifactUpload:
        """Multipart upload (`POST /v1/artifacts` with `request` + `file`)."""
        import json

        request_part = json.dumps(
            {
                "run_id": run_id,
                "name": name,
                "content_type": content_type,
                "source": source,
            }
        )
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/artifacts",
                    files={
                        "request": ("request", request_part, "application/json"),
                        "file": (name, content, content_type),
                    },
                ),
                lambda r: ArtifactUpload.from_dict(_dict(r)),
            )
        )

    def delete(self, artifact_id: str) -> None:
        self._run(
            specs.Op(
                Call("DELETE", f"/v1/artifacts/{seg(artifact_id)}"), lambda _r: None
            )
        )
