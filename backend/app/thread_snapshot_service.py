from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .models import ThreadRecord, TurnRecord


ExtractUserTextFn = Callable[[list[dict[str, Any]]], str]
NowIsoFn = Callable[[], str]


class ThreadSnapshotService:
    def __init__(
        self,
        db: Database,
        *,
        extract_user_text_from_items: ExtractUserTextFn,
        now_iso: NowIsoFn,
    ) -> None:
        self.db = db
        self._extract_user_text_from_items = extract_user_text_from_items
        self._now_iso = now_iso

    def sync_thread_snapshot(
        self,
        codex_thread: dict[str, Any],
        parent_thread_id: str | None = None,
        forked_from_turn_id: str | None = None,
        title: str | None = None,
    ) -> ThreadRecord:
        thread_record = self.thread_record_from_codex(
            codex_thread,
            title=title,
            parent_thread_id=parent_thread_id,
            forked_from_turn_id=forked_from_turn_id,
        )
        self.db.upsert_thread(thread_record)
        for index, turn in enumerate(codex_thread.get("turns", []), start=1):
            existing = self.db.get_turn(thread_record.threadId, turn["id"])
            items = turn.get("items", [])
            self.db.upsert_turn(
                TurnRecord(
                    turnId=turn["id"],
                    threadId=thread_record.threadId,
                    idx=existing.idx if existing else index,
                    userText=existing.userText if existing and existing.userText else self._extract_user_text_from_items(items),
                    status=self.normalize_turn_status(turn.get("status"), fallback=existing.status if existing else "completed"),
                    startedAt=existing.startedAt if existing else thread_record.createdAt,
                    completedAt=existing.completedAt if existing else None,
                    metadata={**(existing.metadata if existing else {}), "items": items},
                )
            )
        return thread_record

    def thread_record_from_codex(
        self,
        codex_thread: dict[str, Any],
        title: str | None = None,
        parent_thread_id: str | None = None,
        forked_from_turn_id: str | None = None,
    ) -> ThreadRecord:
        existing = self.db.get_thread(codex_thread["id"])
        return ThreadRecord(
            threadId=codex_thread["id"],
            title=title or codex_thread.get("name") or codex_thread.get("preview") or (existing.title if existing else "Untitled thread"),
            createdAt=self.from_unix(codex_thread.get("createdAt")),
            updatedAt=self.from_unix(codex_thread.get("updatedAt")),
            parentThreadId=parent_thread_id if parent_thread_id is not None else (existing.parentThreadId if existing else None),
            forkedFromTurnId=forked_from_turn_id if forked_from_turn_id is not None else (existing.forkedFromTurnId if existing else None),
            status=self.normalize_thread_status(codex_thread.get("status"), fallback=existing.status if existing else "idle"),
            metadata={
                "preview": codex_thread.get("preview"),
                "cwd": codex_thread.get("cwd"),
                "path": codex_thread.get("path"),
                "cliVersion": codex_thread.get("cliVersion"),
                "modelProvider": codex_thread.get("modelProvider"),
                "source": codex_thread.get("source"),
                "remoteThreadId": codex_thread.get("id"),
            },
        )

    def update_local_thread_from_codex(self, local_thread_id: str, codex_thread: dict[str, Any]) -> ThreadRecord:
        existing = self.db.get_thread(local_thread_id)
        if not existing:
            return self.thread_record_from_codex(codex_thread)
        metadata = dict(existing.metadata)
        metadata.update(
            {
                "preview": codex_thread.get("preview"),
                "cwd": codex_thread.get("cwd"),
                "path": codex_thread.get("path"),
                "cliVersion": codex_thread.get("cliVersion"),
                "modelProvider": codex_thread.get("modelProvider"),
                "source": codex_thread.get("source"),
                "remoteThreadId": codex_thread.get("id"),
            }
        )
        updated = ThreadRecord(
            threadId=existing.threadId,
            title=codex_thread.get("name") or codex_thread.get("preview") or existing.title,
            createdAt=existing.createdAt,
            updatedAt=self.from_unix(codex_thread.get("updatedAt")),
            parentThreadId=existing.parentThreadId,
            forkedFromTurnId=existing.forkedFromTurnId,
            status=self.normalize_thread_status(codex_thread.get("status"), fallback=existing.status),
            metadata=metadata,
        )
        self.db.upsert_thread(updated)
        return updated

    def normalize_thread_status(self, status: Any, fallback: str = "idle") -> str:
        if isinstance(status, str):
            return status
        if isinstance(status, dict):
            status_type = status.get("type")
            if status_type == "active":
                return "running"
            if status_type == "systemError":
                return "error"
            if isinstance(status_type, str):
                return status_type
        return fallback

    def normalize_turn_status(self, status: Any, fallback: str = "running") -> str:
        if not isinstance(status, str):
            return fallback
        if status == "inProgress":
            return "running"
        if status == "failed":
            return "error"
        if status == "interrupted":
            return "interrupted"
        return status

    def remote_thread_id(self, thread: ThreadRecord) -> str:
        remote_thread_id = thread.metadata.get("remoteThreadId")
        if isinstance(remote_thread_id, str) and remote_thread_id:
            return remote_thread_id
        return thread.threadId

    def from_unix(self, value: int | float | None) -> str:
        if value is None:
            return self._now_iso()
        return datetime.fromtimestamp(float(value), tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
