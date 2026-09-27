import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()


def sanitize(value: Any, key: str = ""):
    sensitive = ("key", "token", "secret", "password", "authorization")
    if any(part in key.casefold() for part in sensitive):
        return "[redacted]"
    if isinstance(value, dict): return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list): return [sanitize(v) for v in value]
    return value


class JsonlTraceWriter:
    def __init__(self, directory: Path | str = "artifacts/traces"):
        self.directory = Path(directory)

    def write(self, row: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).date().isoformat()
        encoded = json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        with _LOCK:
            path = self.directory / f"{day}.jsonl"
            existing = path.read_bytes() if path.exists() else b""
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
                    temp_name = handle.name
                    handle.write(existing)
                    handle.write(encoded.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
                temp_name = None
            finally:
                if temp_name and os.path.exists(temp_name): os.unlink(temp_name)
