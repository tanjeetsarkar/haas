from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    schema = app.openapi()
    schema["paths"] = {
        path: value
        for path, value in schema["paths"].items()
        if path.startswith("/v1/")
    }
    output = Path(__file__).resolve().parents[1] / "docs" / "openapi-v1.json"
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
