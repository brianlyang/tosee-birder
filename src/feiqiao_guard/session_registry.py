from __future__ import annotations

import threading
from collections.abc import Callable

TerminalActionWriter = Callable[[str], None]


class SessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writers: dict[str, TerminalActionWriter] = {}

    def register(self, session_id: str, writer: TerminalActionWriter) -> None:
        with self._lock:
            self._writers[session_id] = writer

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._writers.pop(session_id, None)

    def apply_terminal_action(self, session_id: str, action: str) -> bool:
        with self._lock:
            writer = self._writers.get(session_id)
        if writer is None:
            return False
        writer(action)
        return True

