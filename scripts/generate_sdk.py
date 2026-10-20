#!/usr/bin/env python3
"""
OpenAPI to Python SDK Generator for HaaS.

Generates:
- haas_sdk/models.py - Typed models matching OpenAPI schemas
- haas_sdk/_specs.py - Request builders and response parsers
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

OPENAPI_PATH = Path("/home/voldemort/work/harness/docs/openapi-v1.json")
SDK_ROOT = Path("/home/voldemort/work/harness/haas_sdk")


def load_openapi() -> dict[str, Any]:
    with open(OPENAPI_PATH) as f:
        return json.load(f)


def to_snake_case(name: str) -> str:
    """Convert PascalCase to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def to_pascal_case(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a JSON reference within the spec."""
    if not ref.startswith("#/"):
        return {}
    parts = ref.split("/")[1:]
    obj = spec
    for part in parts:
        obj = obj.get(part, {})
    return obj


def get_schema(spec: dict, schema_ref: dict | str) -> dict:
    """Get schema from either a direct schema object or a $ref."""
    if isinstance(schema_ref, dict):
        return schema_ref
    if isinstance(schema_ref, str) and schema_ref.startswith("#/"):
        return resolve_ref(spec, schema_ref)
    return {}

def map_type(spec: dict, schema: dict, required: set[str] | None = None) -> str:
    """Map OpenAPI schema to Python type annotation."""
    required = required or set()
    if "$ref" in schema:
        ref_name = schema["$ref"].split("/")[-1]
        return ref_name
    schema_type = schema.get("type")
    if schema_type == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "datetime"
        if fmt == "uuid":
            return "str"
        return "str"
    if schema_type == "integer":
        return "int"
    if schema_type == "number":
        return "float"
    if schema_type == "boolean":
        return "bool"
    if schema_type == "array":
        items = schema.get("items", {})
        item_type = map_type(spec, items)
        return f"list[{item_type}]"
    if schema_type == "object":
        if "additionalProperties" in schema:
            add_props = schema["additionalProperties"]
            if isinstance(add_props, dict):
                val_type = map_type(spec, add_props)
                return f"dict[str, {val_type}]"
            return "dict[str, Any]"
        return "dict[str, Any]"
    if "anyOf" in schema:
        types = []
        for opt in schema["anyOf"]:
            if opt.get("type") == "null":
                continue
            types.append(map_type(spec, opt))
        if len(types) == 1:
            return f"{types[0]} | None"
        return " | ".join(types)
    return "Any"

def generate_models(spec: dict) -> str:
    """Generate Python models from OpenAPI components/schemas."""
    schemas = spec.get("components", {}).get("schemas", {})
    skip_prefixes = ("Body_", "HTTPValidationError", "Response ", "ValidationError")
    model_schemas = {
        k: v for k, v in schemas.items()
        if not any(k.startswith(p) for p in skip_prefixes)
    }
    lines = [
        '"""Typed views over HaaS API resources."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass, field",
        "from datetime import datetime",
        "from typing import Any, Generic, TypeVar",
        "",
        "",
        'TERMINAL_EVENTS = frozenset({"run_completed", "run_error", "run_cancelled"})',
        'TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})',
        "# A stream may close while a run is still active;  waits on a human.",
        'ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})',
        "",
        'T = TypeVar("T")',
        "",
    ]
    for name, schema in sorted(model_schemas.items()):
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields = []
        for prop_name, prop_schema in props.items():
            if "$ref" in prop_schema:
                type_ann = prop_schema["$ref"].split("/")[-1]
            else:
                type_ann = map_type(spec, prop_schema, required)
            if prop_name in required:
                fields.append((prop_name, type_ann, True, None))
            else:
                fields.append((prop_name, f"{type_ann} | None", False, None))
        # Sort fields: required first, then optional (to satisfy dataclass ordering)
        fields.sort(key=lambda x: (not x[2], x[0]))
        lines.append("@dataclass(frozen=True)")
        lines.append(f"class {name}:")
        for fname, ftype, req, default in fields:
            if req:
                lines.append(f"    {fname}: {ftype}")
            else:
                lines.append(f"    {fname}: {ftype} = {default}")
        lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
        lines.append("")
        lines.append("    @classmethod")
        lines.append(f"    def from_dict(cls, data: dict[str, Any]) -> {name}:")
        lines.append("        return cls(")
        for fname, ftype, req, default in fields:
            if ftype == "datetime":
                lines.append(f"            {fname}=datetime.fromisoformat(data.get('{fname}', '').replace('Z', '+00:00')) if data.get('{fname}') else None,")
            else:
                lines.append(f"            {fname}=data.get('{fname}'),")
        lines.append("            raw=data,")
        lines.append("        )")
        lines.append("")
    # Add custom models not in OpenAPI spec
    lines.append("")
    lines.append("# Custom models for pagination and webhook handling (not in OpenAPI spec)")
    lines.append("@dataclass(frozen=True)")
    lines.append("class Page(Generic[T]):")
    lines.append("    items: list[T]")
    lines.append("    next_cursor: str | None = None")
    lines.append("")
    lines.append("@dataclass(frozen=True)")
    lines.append("class WebhookEvent:")
    lines.append("    payload: dict[str, Any]")
    lines.append("    delivery_id: str | None = None")
    lines.append("    key_id: str | None = None")
    lines.append("    timestamp: int | None = None")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_payload(cls, body: bytes, *, delivery_id: str | None = None,")
    lines.append("                 key_id: str | None = None, timestamp: int | None = None) -> WebhookEvent:")
    lines.append("        import json")
    lines.append("        payload = json.loads(body.decode())")
    lines.append("        return cls(")
    lines.append("            payload=payload,")
    lines.append("            delivery_id=delivery_id,")
    lines.append("            key_id=key_id,")
    lines.append("            timestamp=timestamp,")
    lines.append("            raw=payload,")
    lines.append("        )")
    lines.append("")

    # Core resource models not in OpenAPI spec
    lines.append("# Core resource models (not in OpenAPI spec)")
    lines.append("@dataclass(frozen=True)")
    lines.append("class Run:")
    lines.append("    run_id: str")
    lines.append("    thread_id: str")
    lines.append("    status: str")
    lines.append("    created_at: datetime")
    lines.append("    updated_at: datetime")
    lines.append("    agent_revision_id: str | None = None")
    lines.append("    prompt: str | None = None")
    lines.append("    error: str | None = None")
    lines.append("    is_terminal: bool = False")
    lines.append("    waiting_for_approval: bool = False")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> Run:")
    lines.append("        return cls(")
    lines.append("            run_id=data.get('run_id', ''),")
    lines.append("            thread_id=data.get('thread_id', ''),")
    lines.append("            agent_revision_id=data.get('agent_revision_id'),")
    lines.append("            status=data.get('status', ''),")
    lines.append("            created_at=datetime.fromisoformat(data.get('created_at', '').replace('Z', '+00:00')) if data.get('created_at') else datetime.min,")
    lines.append("            updated_at=datetime.fromisoformat(data.get('updated_at', '').replace('Z', '+00:00')) if data.get('updated_at') else datetime.min,")
    lines.append("            prompt=data.get('prompt'),")
    lines.append("            error=data.get('error'),")
    lines.append("            is_terminal=data.get('status') in {'completed', 'failed', 'cancelled'},")
    lines.append("            waiting_for_approval=data.get('status') == 'waiting_for_approval',")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")

    lines.append("@dataclass(frozen=True)")
    lines.append("class RunEvent:")
    lines.append("    type: str")
    lines.append("    data: dict[str, Any]")
    lines.append("    sequence: int | None = None")
    lines.append("    id: str | None = None")
    lines.append("    is_terminal: bool = False")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> RunEvent:")
    lines.append("        return cls(")
    lines.append("            type=data.get('type', ''),")
    lines.append("            data=data.get('data', {}),")
    lines.append("            sequence=data.get('sequence'),")
    lines.append("            id=data.get('id'),")
    lines.append("            is_terminal=data.get('type') in {'run_completed', 'run_error', 'run_cancelled'},")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")

    lines.append("@dataclass(frozen=True)")
    lines.append("class Thread:")
    lines.append("    thread_id: str")
    lines.append("    created_at: datetime")
    lines.append("    updated_at: datetime")
    lines.append("    title: str | None = None")
    lines.append("    metadata: dict[str, Any] | None = None")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> Thread:")
    lines.append("        return cls(")
    lines.append("            thread_id=data.get('thread_id', ''),")
    lines.append("            created_at=datetime.fromisoformat(data.get('created_at', '').replace('Z', '+00:00')) if data.get('created_at') else datetime.min,")
    lines.append("            updated_at=datetime.fromisoformat(data.get('updated_at', '').replace('Z', '+00:00')) if data.get('updated_at') else datetime.min,")
    lines.append("            title=data.get('title'),")
    lines.append("            metadata=data.get('metadata'),")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")
    
    lines.append("@dataclass(frozen=True)")
    lines.append("class AgentRevision:")
    lines.append("    revision_id: str")
    lines.append("    agent_name: str")
    lines.append("    version: str")
    lines.append("    state: str")
    lines.append("    created_at: datetime")
    lines.append("    config: dict[str, Any] | None = None")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> AgentRevision:")
    lines.append("        return cls(")
    lines.append("            revision_id=data.get('revision_id', ''),")
    lines.append("            agent_name=data.get('agent_name', ''),")
    lines.append("            version=data.get('version', ''),")
    lines.append("            state=data.get('state', ''),")
    lines.append("            created_at=datetime.fromisoformat(data.get('created_at', '').replace('Z', '+00:00')) if data.get('created_at') else datetime.min,")
    lines.append("            config=data.get('config'),")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")
    
    lines.append("@dataclass(frozen=True)")
    lines.append("class ApprovalBatch:")
    lines.append("    batch_id: str")
    lines.append("    run_id: str")
    lines.append("    status: str")
    lines.append("    batch_digest: str")
    lines.append("    created_at: datetime")
    lines.append("    expires_at: datetime | None = None")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> ApprovalBatch:")
    lines.append("        return cls(")
    lines.append("            batch_id=data.get('batch_id', ''),")
    lines.append("            run_id=data.get('run_id', ''),")
    lines.append("            status=data.get('status', ''),")
    lines.append("            batch_digest=data.get('batch_digest', ''),")
    lines.append("            created_at=datetime.fromisoformat(data.get('created_at', '').replace('Z', '+00:00')) if data.get('created_at') else datetime.min,")
    lines.append("            expires_at=datetime.fromisoformat(data.get('expires_at', '').replace('Z', '+00:00')) if data.get('expires_at') else None,")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")
    
    lines.append("@dataclass(frozen=True)")
    lines.append("class Artifact:")
    lines.append("    artifact_id: str")
    lines.append("    run_id: str")
    lines.append("    filename: str")
    lines.append("    content_type: str")
    lines.append("    size: int")
    lines.append("    created_at: datetime")
    lines.append("    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def from_dict(cls, data: dict[str, Any]) -> Artifact:")
    lines.append("        return cls(")
    lines.append("            artifact_id=data.get('artifact_id', ''),")
    lines.append("            run_id=data.get('run_id', ''),")
    lines.append("            filename=data.get('filename', ''),")
    lines.append("            content_type=data.get('content_type', ''),")
    lines.append("            size=data.get('size', 0),")
    lines.append("            created_at=datetime.fromisoformat(data.get('created_at', '').replace('Z', '+00:00')) if data.get('created_at') else datetime.min,")
    lines.append("            raw=data,")
    lines.append("        )")
    lines.append("")

    return "\n".join(lines)


def generate_specs(spec: dict) -> str:
    """Generate _specs.py from OpenAPI paths."""
    paths = spec.get("paths", {})
    
    # First pass: collect all model names used in responses
    used_models = set()
    resources = {}
    for path, methods in paths.items():
        parts = path.strip("/").split("/")
        if parts[0] == "v1":
            resource = parts[1] if len(parts) > 1 else "root"
        else:
            resource = parts[0]
        if resource not in resources:
            resources[resource] = []
        for method, op in methods.items():
            resources[resource].append((path, method, op))
    
    for resource, ops in sorted(resources.items()):
        for path, method, op in ops:
            op_id = op.get("operationId", "")
            if not op_id:
                continue
            responses = op.get("responses", {})
            success_response = responses.get("200") or responses.get("201") or responses.get("202")
            if success_response:
                content = success_response.get("content", {})
                json_content = content.get("application/json", {})
                schema_ref = json_content.get("schema", {})
                if "$ref" in schema_ref:
                    ref_name = schema_ref["$ref"].split("/")[-1]
                    if ref_name.endswith("Response") or ref_name.endswith("ListResponse"):
                        used_models.add(ref_name)
    
    # Build model imports
    model_imports = ["    Page,"]
    for model in sorted(used_models):
        model_imports.append(f"    {model},")
    
    lines = [
        '"""Request builders and response parsers shared by',
        '    :class:`~haas_sdk.Client` and :class:`~haas_sdk.AsyncClient`.',
        '',
        'Each builder returns an :class:`~haas_sdk._core.Op`',
        '(what to send, how to read the answer) so the',
        'sync and async clients only differ in how they perform I/O.',
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Callable, Iterable",
        "from dataclasses import dataclass",
        "from datetime import datetime",
        "from typing import Any, Generic, TypeVar",
        "",
        "import httpx",
        "",
        "from haas_sdk._core import Call, clean, idempotency_headers, seg",
        "from haas_sdk.models import (",
        *model_imports,
        ")",
        "",
        'T = TypeVar("T")',
        "",
        'APPROVAL_DECISIONS = frozenset({"approve", "reject"})',
        "",
        "",
        "@dataclass",
        "class Op(Generic[T]):",
        "    call: Call",
        "    parse: Callable[[httpx.Response], T]",
        "",
        "",
        "def _json(response: httpx.Response) -> Any:",
        "    if not response.content:",
        "        return {}",
        "    return response.json()",
        "",
        "",
        "def _dict(response: httpx.Response) -> dict[str, Any]:",
        "    body = _json(response)",
        '    return body if isinstance(body, dict) else {"value": body}',
        "",
        "",
        "def _page(factory: Callable[[dict[str, Any]], T]) -> Callable[[httpx.Response], Page[T]]:",
        "    def parse(response: httpx.Response) -> Page[T]:",
        "        body = _dict(response)",
        "        return Page(",
        '            items=[factory(item) for item in body.get("items") or []],',
        '            next_cursor=body.get("next_cursor"),',
        "        )",
        "    return parse",
        "",
        "",
        "def _when(value: datetime | float | int | str | None) -> str | None:",
        "    if value is None:",
        "        return None",
        "    if isinstance(value, datetime):",
        "        return value.isoformat()",
        "    return str(value)",
        "",
        "",
        "def _multi(value: str | Iterable[str] | None) -> str | None:",
        "    if value is None:",
        "        return None",
        "    if isinstance(value, str):",
        "        return value",
        '    return ",".join(value)',
        "",
    ]
    resources = {}
    for path, methods in paths.items():
        parts = path.strip("/").split("/")
        if parts[0] == "v1":
            resource = parts[1] if len(parts) > 1 else "root"
        else:
            resource = parts[0]
        if resource not in resources:
            resources[resource] = []
        for method, op in methods.items():
            resources[resource].append((path, method, op))
    for resource, ops in sorted(resources.items()):
        lines.append(f"# --- {resource} ---")
        lines.append("")
        for path, method, op in ops:
            op_id = op.get("operationId", "")
            if not op_id:
                continue
            func_name = to_snake_case(op_id)
            if func_name.endswith("_v1"):
                func_name = func_name[:-3]
            params = op.get("parameters", [])
            path_params = [p for p in params if p["in"] == "path"]
            query_params = [p for p in params if p["in"] == "query"]
            _ = [p for p in params if p["in"] == "header"]
            request_body = op.get("requestBody", {})
            has_body = bool(request_body)
            responses = op.get("responses", {})
            success_response = responses.get("200") or responses.get("201") or responses.get("202")
            is_idempotent = method.upper() in ("GET", "DELETE", "HEAD", "OPTIONS")
            lines.append(f"def {func_name}(")
            for pp in path_params:
                lines.append(f"    {pp['name']}: str,")
            for qp in query_params:
                qp_name = qp["name"]
                qp_schema = qp.get("schema", {})
                qp_type = map_type(spec, qp_schema)
                default = qp_schema.get("default")
                if default is not None:
                    if isinstance(default, str):
                        lines.append(f"    {qp_name}: {qp_type} | None = '{default}',")
                    else:
                        lines.append(f"    {qp_name}: {qp_type} | None = {default},")
                else:
                    lines.append(f"    {qp_name}: {qp_type} | None = None,")
            if not is_idempotent:
                lines.append("    idempotency_key: str | None = None,")
            if has_body:
                lines.append("    **body: Any,")
            lines.append(") -> Op[dict[str, Any]]:  # TODO: proper return type")
            summary = op.get('summary', op_id).replace('"""', '"')
            lines.append(f'    """{summary}"""')
            url_path = path
            for pp in path_params:
                url_path = url_path.replace(f"{{{pp['name']}}}", f"{{seg({pp['name']})}}")
            lines.append("    call = Call(")
            lines.append(f'        "{method.upper()}",')
            if "{" in url_path:
                lines.append(f'        f"{url_path}",')
            else:
                lines.append(f'        "{url_path}",')
            if query_params:
                lines.append("        params=clean({")
                for qp in query_params:
                    lines.append(f'            "{qp["name"]}": {qp["name"]},')
                lines.append("        }),")
            else:
                lines.append("        params=None,")
            if has_body:
                lines.append("        json=body,")
            else:
                lines.append("        json=None,")
            header_lines = ["        headers={"]
            if not is_idempotent:
                header_lines.append("            **idempotency_headers(idempotency_key),")
            header_lines.append("        },")
            lines.extend(header_lines)
            lines.append(f"        idempotent={str(is_idempotent)},")
            lines.append("    )")
            if success_response:
                content = success_response.get("content", {})
                json_content = content.get("application/json", {})
                schema_ref = json_content.get("schema", {})
                if "$ref" in schema_ref:
                    ref_name = schema_ref["$ref"].split("/")[-1]
                    if ref_name.endswith("Response") or ref_name.endswith("ListResponse"):
                        if "List" in ref_name or "Page" in ref_name:
                            lines.append(f"    return Op(call, _page({ref_name}.from_dict))")
                        else:
                            lines.append(f"    return Op(call, lambda r: {ref_name}.from_dict(_dict(r)))")
                    else:
                        lines.append("    return Op(call, _dict)")
                else:
                    lines.append("    return Op(call, _dict)")
            else:
                lines.append("    return Op(call, _dict)")
            lines.append("")
    return "\n".join(lines)

def main() -> None:
    spec = load_openapi()
    models_code = generate_models(spec)
    models_path = SDK_ROOT / "models.py"
    with open(models_path, "w") as f:
        f.write(models_code)
    print(f"Generated {models_path}")
    specs_code = generate_specs(spec)
    specs_path = SDK_ROOT / "_specs.py"
    with open(specs_path, "w") as f:
        f.write(specs_code)
    print(f"Generated {specs_path}")


if __name__ == "__main__":
    main()
