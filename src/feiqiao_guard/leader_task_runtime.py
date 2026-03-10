from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from .models import LeaderCommandRequest, LeaderCommandResponse


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dump_model(model: LeaderCommandResponse) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()  # type: ignore[attr-defined]


class LeaderTaskRuntime:
    def __init__(self, *, retention_seconds: int = 86400, max_items: int = 1000) -> None:
        self._retention_seconds = max(60, int(retention_seconds))
        self._max_items = max(100, int(max_items))
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}

    def submit(
        self,
        *,
        request: LeaderCommandRequest,
        runner: Callable[[LeaderCommandRequest], LeaderCommandResponse],
    ) -> dict:
        task_id = uuid.uuid4().hex
        now = _utcnow()
        record = {
            "task_id": task_id,
            "state": "accepted",
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "error": None,
            "result": None,
            "events": [
                {
                    "at": now,
                    "state": "accepted",
                    "note": "task_submitted",
                }
            ],
        }
        with self._lock:
            self._prune_locked(now=now)
            self._tasks[task_id] = record
            self._prune_max_items_locked()
            accepted_snapshot = deepcopy(record)

        worker = threading.Thread(
            target=self._run_task,
            kwargs={"task_id": task_id, "request": request, "runner": runner},
            daemon=True,
            name=f"fqg-leader-task-{task_id[:8]}",
        )
        worker.start()
        return accepted_snapshot

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            self._prune_locked(now=_utcnow())
            current = self._tasks.get(task_id)
            if current is None:
                return None
            return deepcopy(current)

    def _run_task(
        self,
        *,
        task_id: str,
        request: LeaderCommandRequest,
        runner: Callable[[LeaderCommandRequest], LeaderCommandResponse],
    ) -> None:
        self._transition(task_id=task_id, state="running", note="dispatch_started")
        try:
            result = runner(request)
        except Exception as exc:  # noqa: BLE001
            self._transition(
                task_id=task_id,
                state="failed",
                note="dispatch_exception",
                error=f"{type(exc).__name__}:{exc}",
            )
            return

        if result.accepted:
            self._transition(
                task_id=task_id,
                state="final",
                note="dispatch_completed",
                result=_dump_model(result),
            )
            return
        self._transition(
            task_id=task_id,
            state="failed",
            note="dispatch_unaccepted",
            result=_dump_model(result),
        )

    def _transition(
        self,
        *,
        task_id: str,
        state: str,
        note: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        now = _utcnow()
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record["state"] = state
            record["updated_at"] = now
            if state in {"final", "failed", "timeout"}:
                record["finished_at"] = now
            if result is not None:
                record["result"] = result
            if error:
                record["error"] = error
            record["events"].append({"at": now, "state": state, "note": note})

    def _prune_locked(self, *, now: datetime) -> None:
        stale_ids: list[str] = []
        now_ts = now.timestamp()
        for task_id, record in self._tasks.items():
            state = str(record.get("state", "")).strip().lower()
            if state not in {"final", "failed", "timeout"}:
                continue
            finished_at = record.get("finished_at")
            if not isinstance(finished_at, datetime):
                continue
            age = now_ts - finished_at.timestamp()
            if age > self._retention_seconds:
                stale_ids.append(task_id)
        for task_id in stale_ids:
            self._tasks.pop(task_id, None)

    def _prune_max_items_locked(self) -> None:
        if len(self._tasks) <= self._max_items:
            return
        ranked = sorted(
            self._tasks.items(),
            key=lambda kv: (
                kv[1].get("updated_at", datetime.fromtimestamp(0, timezone.utc)),
                kv[0],
            ),
            reverse=True,
        )
        keep_ids = {task_id for task_id, _ in ranked[: self._max_items]}
        stale_ids = [task_id for task_id in self._tasks if task_id not in keep_ids]
        for task_id in stale_ids:
            self._tasks.pop(task_id, None)
