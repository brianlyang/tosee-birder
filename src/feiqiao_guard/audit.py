from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditStore:
    def __init__(self, output_path: Path) -> None:
        self._path = output_path.expanduser().resolve()
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, payload: dict[str, Any]) -> None:
        row = {
            "event": event,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        data = json.dumps(row, ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(data + "\n")
