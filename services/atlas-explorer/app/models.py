"""Strict, bounded version-one workspace request and mutation models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

MAX_TITLE = 200
MAX_DRAFT = 32_000
MAX_MUTATION_BYTES = 256 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceCreate(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    mutation_id: Annotated[str, Field(min_length=1, max_length=128)]
    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE)]
    draft: Annotated[str, Field(max_length=MAX_DRAFT)]


class MutationKind(StrEnum):
    DRAFT_CHECKPOINT = "draft_checkpoint"
    QUERY_SUBMITTED = "query_submitted"
    SELECTION_CHANGED = "selection_changed"
    BRANCH_COMMITTED = "branch_committed"
    COMPARISON_CHANGED = "comparison_changed"
    ACTION_DRAFT_CHANGED = "action_draft_changed"
    OPERATION_PREPARED = "operation_prepared"
    OPERATION_BOUND = "operation_bound"
    OPERATION_OUTCOME = "operation_outcome"


class DraftPayload(StrictModel):
    draft: Annotated[str, Field(max_length=MAX_DRAFT)]


class PromptPayload(StrictModel):
    prompt: Annotated[str, Field(min_length=1, max_length=MAX_DRAFT)]


class SelectionPayload(StrictModel):
    selection: dict[str, Any]


class BranchPayload(StrictModel):
    branch: dict[str, Any]


class ComparisonPayload(StrictModel):
    comparison: dict[str, Any]


class ActionDraftPayload(StrictModel):
    action_draft: dict[str, Any]


class PreparedPayload(StrictModel):
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    prompt: Annotated[str, Field(min_length=1, max_length=MAX_DRAFT)]
    selected_context: list[dict[str, JsonValue]]


class BoundPayload(StrictModel):
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    run_id: Annotated[str, Field(min_length=1, max_length=256)]
    thread_id: Annotated[str | None, Field(max_length=256)] = None


class OperationState(StrEnum):
    SUBMITTING = "submitting"
    BOUND = "bound"
    UNKNOWN = "unknown"
    TERMINAL = "terminal"


class OutcomePayload(StrictModel):
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    state: str
    status: Annotated[str, Field(min_length=1, max_length=64)]
    run_id: Annotated[str | None, Field(min_length=1, max_length=256)] = None

    @field_validator("state")
    @classmethod
    def valid_state(cls, value: str) -> str:
        parsed = OperationState(value)
        if parsed == OperationState.BOUND:
            raise ValueError("Use operation_bound to bind a run.")
        return parsed.value

    @field_validator("run_id")
    @classmethod
    def run_id_only_for_unknown(cls, value: str | None, info) -> str | None:
        if value is not None and info.data.get("state") != "unknown":
            raise ValueError("run_id is permitted only for an unknown outcome.")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str, info) -> str:
        state = info.data.get("state")
        allowed = {
            "submitting": {"submitting"},
            "unknown": {"unknown"},
            "terminal": {"completed", "failed", "cancelled"},
        }
        if value not in allowed.get(state, set()):
            raise ValueError("Operation status does not match its state.")
        return value


_PAYLOADS: dict[MutationKind, type[StrictModel]] = {
    MutationKind.DRAFT_CHECKPOINT: DraftPayload,
    MutationKind.QUERY_SUBMITTED: PromptPayload,
    MutationKind.SELECTION_CHANGED: SelectionPayload,
    MutationKind.BRANCH_COMMITTED: BranchPayload,
    MutationKind.COMPARISON_CHANGED: ComparisonPayload,
    MutationKind.ACTION_DRAFT_CHANGED: ActionDraftPayload,
    MutationKind.OPERATION_PREPARED: PreparedPayload,
    MutationKind.OPERATION_BOUND: BoundPayload,
    MutationKind.OPERATION_OUTCOME: OutcomePayload,
}


class WorkspaceMutation(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    mutation_id: Annotated[str, Field(min_length=1, max_length=128)]
    expected_revision: Annotated[int, Field(ge=1)]
    kind: str
    payload: dict[str, Any]

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        return MutationKind(value).value

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_MUTATION_BYTES:
            raise ValueError("Mutation payload exceeds 256 KiB.")
        return value

    def strict_payload(self) -> StrictModel:
        return _PAYLOADS[MutationKind(self.kind)].model_validate(self.payload)

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mutation_id": self.mutation_id,
            "expected_revision": self.expected_revision,
            "kind": MutationKind(self.kind).value,
            "payload": self.strict_payload().model_dump(mode="json"),
        }


PageLimit = Annotated[int, Field(ge=1, le=100)]
