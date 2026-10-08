from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def command_id(prefix: str = "cmd") -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}_{timestamp}_{uuid4().hex[:10]}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def redact_url(url: str) -> str:
    """Remove URL user-info so credentials never reach logs."""
    value = re.sub(
        r"(?P<scheme>^[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]+@", r"\g<scheme>", url
    )
    return re.sub(
        r"(?i)([?&](?:api[_-]?key|key|token|secret|password|signature|credential)=)[^&#\s]+",
        r"\1[redacted]",
        value,
    )


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE lines into os.environ without overriding existing values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
