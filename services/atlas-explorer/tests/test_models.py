"""Strict, versioned workspace mutation payload validation."""

import pytest
from pydantic import ValidationError

from app.models import WorkspaceCreate, WorkspaceMutation
from app.storage import apply_mutation
from fastapi import HTTPException


def test_workspace_creation_is_versioned_and_bounded() -> None:
    created = WorkspaceCreate(
        schema_version=1,
        mutation_id="create-1",
        title="Exploration",
        draft="",
    )
    assert created.schema_version == 1
    with pytest.raises(ValidationError):
        WorkspaceCreate(
            schema_version=1,
            mutation_id="x",
            title="x",
            draft="x",
            tenant_id="forged",
        )


def test_mutation_payload_is_strict_and_canonical() -> None:
    mutation = WorkspaceMutation(
        schema_version=1,
        mutation_id="checkpoint-1",
        expected_revision=1,
        kind="draft_checkpoint",
        payload={"draft": "saved"},
    )
    assert mutation.canonical()["payload"] == {"draft": "saved"}
    with pytest.raises(ValidationError):
        mutation.strict_payload().model_validate({"draft": "saved", "run_id": "forged"})
    with pytest.raises(ValidationError):
        WorkspaceMutation(
            schema_version=1,
            mutation_id="checkpoint-2",
            expected_revision=1,
            kind="draft_checkpoint",
            payload={"draft": "saved", "extra": True},
        ).strict_payload()


def test_operation_dispatch_binding_and_outcome_transitions_are_ordered() -> None:
    state: dict = {}
    apply_mutation(state, "operation_prepared", {
        "operation_id": "op-1",
        "prompt": "exact public prompt",
        "selected_context": [{"entity_id": "public-item"}],
    })
    with pytest.raises(HTTPException):
        apply_mutation(state, "operation_bound", {
            "operation_id": "op-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
        })
    apply_mutation(state, "operation_outcome", {
        "operation_id": "op-1",
        "state": "submitting",
        "status": "submitting",
    })
    apply_mutation(state, "operation_bound", {
        "operation_id": "op-1",
        "run_id": "run-1",
        "thread_id": "thread-1",
    })
    assert state["operations"]["op-1"]["run_id"] == "run-1"
    assert state["operations"]["op-1"]["state"] == "bound"


def test_unresolved_operation_blocks_new_dispatch_and_unknown_requires_recovery() -> None:
    state: dict = {}
    prepared = {
        "operation_id": "op-1",
        "prompt": "exact public prompt",
        "selected_context": [{"entity_id": "public-item"}],
    }
    apply_mutation(state, "operation_prepared", prepared)
    with pytest.raises(HTTPException):
        apply_mutation(state, "operation_prepared", {**prepared, "operation_id": "op-2"})
    with pytest.raises(HTTPException):
        apply_mutation(state, "operation_outcome", {
            "operation_id": "op-1",
            "state": "unknown",
            "status": "unknown",
            "run_id": "recovered-run",
        })
    apply_mutation(state, "operation_outcome", {
        "operation_id": "op-1",
        "state": "submitting",
        "status": "submitting",
    })
    apply_mutation(state, "operation_outcome", {
        "operation_id": "op-1",
        "state": "unknown",
        "status": "unknown",
    })
    with pytest.raises(HTTPException):
        apply_mutation(state, "operation_outcome", {
            "operation_id": "op-1",
            "state": "submitting",
            "status": "submitting",
        })
    apply_mutation(state, "operation_bound", {
        "operation_id": "op-1",
        "run_id": "recovered-run",
        "thread_id": "thread-1",
    })
    assert state["operations"]["op-1"]["state"] == "bound"
    apply_mutation(state, "operation_prepared", {**prepared, "operation_id": "op-3"})
    assert state["operations"]["op-3"]["state"] == "prepared"


def test_unknown_run_binding_is_immutable() -> None:
    state: dict = {}
    apply_mutation(state, "operation_prepared", {
        "operation_id": "op-1",
        "prompt": "prompt",
        "selected_context": [],
    })
    apply_mutation(state, "operation_outcome", {
        "operation_id": "op-1",
        "state": "submitting",
        "status": "submitting",
    })
    apply_mutation(state, "operation_outcome", {
        "operation_id": "op-1",
        "state": "unknown",
        "status": "unknown",
        "run_id": "known-run",
    })
    with pytest.raises(HTTPException) as mismatch:
        apply_mutation(state, "operation_bound", {
            "operation_id": "op-1",
            "run_id": "different-run",
        })
    assert mismatch.value.detail == {"code": "operation_run_id_immutable"}
    apply_mutation(state, "operation_bound", {
        "operation_id": "op-1",
        "run_id": "known-run",
        "thread_id": None,
    })
    assert state["operations"]["op-1"]["run_id"] == "known-run"


def test_operation_payload_shapes_match_041_and_reject_extras() -> None:
    prepared = WorkspaceMutation(
        schema_version=1,
        mutation_id="prepare-good",
        expected_revision=1,
        kind="operation_prepared",
        payload={
            "operation_id": "op",
            "prompt": "prompt",
            "selected_context": [{"entity_id": "public"}],
        },
    )
    assert prepared.strict_payload().model_dump() == prepared.payload
    with pytest.raises(ValidationError):
        WorkspaceMutation(
            schema_version=1,
            mutation_id="prepare-bad",
            expected_revision=1,
            kind="operation_prepared",
            payload={
                "operation_id": "op",
                "prompt": "prompt",
                "selected_context": [],
                "intent_digest": "client-owned",
            },
        ).strict_payload()
    with pytest.raises(ValidationError):
        WorkspaceMutation(
            schema_version=1,
            mutation_id="unknown-run-invalid",
            expected_revision=1,
            kind="operation_outcome",
            payload={
                "operation_id": "op",
                "state": "terminal",
                "status": "completed",
                "run_id": "client-supplied",
            },
        ).strict_payload()
