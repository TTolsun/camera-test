"""Read and atomically replace JSON files used for reports and persistent state."""

import json
import os
from pathlib import Path
import tempfile

from .adb import RunError


def read_json_file(path, code, what):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RunError(code, f"{what} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(code, f"{what} must be a JSON object.")
    return data


def write_json_atomic(path, data):
    """Write through a temp file in the same folder and replace, so readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
