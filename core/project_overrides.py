"""Load machine-local author/project overrides kept outside source control."""

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".run" / "project_overrides.json"


def load_project_overrides() -> dict[str, Any]:
    override_path = Path(os.getenv("LONG_NOVEL_PROJECT_OVERRIDES") or DEFAULT_PATH).expanduser()
    try:
        payload = json.loads(override_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def override_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
