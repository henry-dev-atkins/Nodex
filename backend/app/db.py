from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import ApprovalRecord, EventRecord, ImportPreviewRecord, ThreadRecord, TurnRecord
from .util import ensure_directory, json_dumps, utc_now


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if raw in {None, ""}:
        return fallback
    return json.loads(raw)


class Database:
    def __init__(self, path: Path) -> None:
        ensure_directory(path.parent)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._migrate_turns_schema_if_needed()
            self._migrate_import_previews_schema_if_needed()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads(
                    thread_id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    parent_thread_id TEXT,
                    forked_from_turn_id TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns(
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    user_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(thread_id, turn_id)
                );

                CREATE TABLE IF NOT EXISTS events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals(
                    approval_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    item_id TEXT,
                    request_id TEXT NOT NULL,
                    request_method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS import_previews(
                    preview_id TEXT PRIMARY KEY,
                    dest_thread_id TEXT NOT NULL,
                    dest_turn_id TEXT,
                    source_thread_id TEXT NOT NULL,
                    source_turn_ids_json TEXT NOT NULL,
                    suspected_secrets_json TEXT NOT NULL,
                    transfer_blob TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_thread_turn_seq ON events(thread_id, turn_id, seq);
                CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);
                CREATE INDEX IF NOT EXISTS idx_turns_thread_idx ON turns(thread_id, idx);
                CREATE INDEX IF NOT EXISTS idx_turns_turn_id ON turns(turn_id);
                CREATE INDEX IF NOT EXISTS idx_threads_updated_at ON threads(updated_at DESC);
                """
            )
            self._conn.commit()

    def _migrate_turns_schema_if_needed(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(turns)").fetchall()
        if not rows:
            return
        pk_columns = [row["name"] for row in sorted(rows, key=lambda item: int(item["pk"])) if int(row["pk"]) > 0]
        if pk_columns == ["thread_id", "turn_id"]:
            return
        self._conn.executescript(
            """
            ALTER TABLE turns RENAME TO turns_legacy;

            CREATE TABLE turns(
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(thread_id, turn_id)
            );

            INSERT INTO turns(thread_id, turn_id, idx, user_text, status, started_at, completed_at, metadata_json)
            SELECT thread_id, turn_id, idx, user_text, status, started_at, completed_at, metadata_json
            FROM turns_legacy;

            DROP TABLE turns_legacy;
            """
        )

    def _migrate_import_previews_schema_if_needed(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(import_previews)").fetchall()
        if not rows:
            return
        columns = {row["name"] for row in rows}
        if "dest_turn_id" not in columns:
            self._conn.execute("ALTER TABLE import_previews ADD COLUMN dest_turn_id TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_thread(self, thread: ThreadRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads(thread_id, title, created_at, updated_at, parent_thread_id, forked_from_turn_id, status, metadata_json)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title=excluded.title,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    parent_thread_id=COALESCE(excluded.parent_thread_id, threads.parent_thread_id),
                    forked_from_turn_id=COALESCE(excluded.forked_from_turn_id, threads.forked_from_turn_id),
                    status=excluded.status,
                    metadata_json=excluded.metadata_json
                """,
                (
                    thread.threadId,
                    thread.title,
                    thread.createdAt,
                    thread.updatedAt,
                    thread.parentThreadId,
                    thread.forkedFromTurnId,
                    thread.status,
                    json_dumps(thread.metadata),
                ),
            )
            self._conn.commit()

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return self._row_to_thread(row) if row else None

    def list_threads(self) -> list[ThreadRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM threads ORDER BY updated_at DESC, created_at DESC").fetchall()
        return [self._row_to_thread(row) for row in rows]

    def update_thread_status(self, thread_id: str, status: str, metadata: dict[str, Any] | None = None) -> ThreadRecord | None:
        thread = self.get_thread(thread_id)
        if not thread:
            return None
        thread.status = status
        thread.updatedAt = utc_now()
        if metadata:
            next_metadata = dict(thread.metadata)
            next_metadata.update(metadata)
            thread.metadata = next_metadata
        self.upsert_thread(thread)
        return thread

    def upsert_turn(self, turn: TurnRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO turns(thread_id, turn_id, idx, user_text, status, started_at, completed_at, metadata_json)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id, turn_id) DO UPDATE SET
                    idx=excluded.idx,
                    user_text=excluded.user_text,
                    status=excluded.status,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    turn.threadId,
                    turn.turnId,
                    turn.idx,
                    turn.userText,
                    turn.status,
                    turn.startedAt,
                    turn.completedAt,
                    json_dumps(turn.metadata),
                ),
            )
            self._conn.commit()

    def get_turn(self, thread_id: str, turn_id: str) -> TurnRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            ).fetchone()
        return self._row_to_turn(row) if row else None

    def list_turns(self, thread_id: str) -> list[TurnRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM turns WHERE thread_id = ? ORDER BY idx ASC", (thread_id,)).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def get_next_turn_index(self, thread_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(idx), 0) AS max_idx FROM turns WHERE thread_id = ?", (thread_id,)).fetchone()
        return int(row["max_idx"]) + 1

    def get_last_turn_id(self, thread_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT turn_id FROM turns WHERE thread_id = ? ORDER BY idx DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return str(row["turn_id"]) if row else None

    def update_turn_status(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
        completed_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnRecord | None:
        turn = self.get_turn(thread_id, turn_id)
        if not turn:
            return None
        turn.status = status
        if completed_at is not None:
            turn.completedAt = completed_at
        if metadata:
            next_metadata = dict(turn.metadata)
            next_metadata.update(metadata)
            turn.metadata = next_metadata
        self.upsert_turn(turn)
        return turn

    def append_event(self, thread_id: str, turn_id: str | None, seq: int, event_type: str, payload: dict[str, Any], ts: str | None = None) -> EventRecord:
        timestamp = ts or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events(thread_id, turn_id, seq, type, ts, payload_json)
                VALUES(?,?,?,?,?,?)
                """,
                (thread_id, turn_id, seq, event_type, timestamp, json_dumps(payload)),
            )
            self._conn.commit()
            event_id = int(cursor.lastrowid)
        return EventRecord(eventId=event_id, threadId=thread_id, turnId=turn_id, seq=seq, type=event_type, ts=timestamp, payload=payload)

    def list_events(self, after_event_id: int | None = None, thread_id: str | None = None, limit: int = 5000) -> list[EventRecord]:
        query = "SELECT * FROM events"
        clauses: list[str] = []
        args: list[Any] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            args.append(thread_id)
        if after_event_id is not None:
            clauses.append("event_id > ?")
            args.append(after_event_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_turn_events(self, thread_id: str, turn_id: str) -> list[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE thread_id = ? AND turn_id = ? ORDER BY event_id ASC",
                (thread_id, turn_id),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def last_event_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(event_id), 0) AS max_id FROM events").fetchone()
        return int(row["max_id"])

    def upsert_approval(self, approval: ApprovalRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO approvals(approval_id, thread_id, turn_id, item_id, request_id, request_method, status, details_json, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status=excluded.status,
                    details_json=excluded.details_json,
                    updated_at=excluded.updated_at
                """,
                (
                    approval.approvalId,
                    approval.threadId,
                    approval.turnId,
                    approval.itemId,
                    approval.requestId,
                    approval.requestMethod,
                    approval.status,
                    json_dumps(approval.details),
                    approval.createdAt,
                    approval.updatedAt,
                ),
            )
            self._conn.commit()

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return self._row_to_approval(row) if row else None

    def list_pending_approvals(self) -> list[ApprovalRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        return [self._row_to_approval(row) for row in rows]

    def list_approvals(self, thread_id: str | None = None, turn_id: str | None = None) -> list[ApprovalRecord]:
        query = "SELECT * FROM approvals"
        clauses: list[str] = []
        args: list[Any] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            args.append(thread_id)
        if turn_id is not None:
            clauses.append("turn_id = ?")
            args.append(turn_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, approval_id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def update_approval_status(self, approval_id: str, status: str) -> ApprovalRecord | None:
        approval = self.get_approval(approval_id)
        if not approval:
            return None
        approval.status = status  # type: ignore[assignment]
        approval.updatedAt = utc_now()
        self.upsert_approval(approval)
        return approval

    def save_import_preview(self, preview: ImportPreviewRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO import_previews(
                    preview_id,
                    dest_thread_id,
                    dest_turn_id,
                    source_thread_id,
                    source_turn_ids_json,
                    suspected_secrets_json,
                    transfer_blob,
                    expires_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    preview.previewId,
                    preview.destThreadId,
                    preview.destTurnId,
                    preview.sourceThreadId,
                    json_dumps(preview.sourceTurnIds),
                    json_dumps(preview.suspectedSecrets),
                    preview.transferBlob,
                    preview.expiresAt,
                ),
            )
            self._conn.commit()

    def get_import_preview(self, preview_id: str) -> ImportPreviewRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM import_previews WHERE preview_id = ?", (preview_id,)).fetchone()
        if not row:
            return None
        return ImportPreviewRecord(
            previewId=row["preview_id"],
            destThreadId=row["dest_thread_id"],
            destTurnId=row["dest_turn_id"],
            sourceThreadId=row["source_thread_id"],
            sourceTurnIds=_json_loads(row["source_turn_ids_json"], []),
            suspectedSecrets=_json_loads(row["suspected_secrets_json"], []),
            transferBlob=row["transfer_blob"],
            expiresAt=row["expires_at"],
        )

    def delete_import_preview(self, preview_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM import_previews WHERE preview_id = ?", (preview_id,))
            self._conn.commit()

    def delete_expired_import_previews(self, now_iso: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM import_previews WHERE expires_at < ?", (now_iso,))
            self._conn.commit()

    def list_conversation_thread_ids(self, thread_id: str) -> list[str]:
        threads = self.list_threads()
        by_id = {thread.threadId: thread for thread in threads}
        root_id = thread_id
        while True:
            current = by_id.get(root_id)
            if not current or not current.parentThreadId:
                break
            root_id = current.parentThreadId
        children_by_parent: dict[str, list[str]] = {}
        for thread in threads:
            if not thread.parentThreadId:
                continue
            children_by_parent.setdefault(thread.parentThreadId, []).append(thread.threadId)
        ordered_ids: list[str] = []
        stack = [root_id]
        while stack:
            current = stack.pop()
            if current in ordered_ids:
                continue
            ordered_ids.append(current)
            for child_id in reversed(children_by_parent.get(current, [])):
                stack.append(child_id)
        return ordered_ids

    def delete_threads(self, thread_ids: list[str]) -> None:
        if not thread_ids:
            return
        placeholders = ",".join("?" for _ in thread_ids)
        params = tuple(thread_ids)
        with self._lock:
            self._conn.execute(
                f"DELETE FROM import_previews WHERE dest_thread_id IN ({placeholders}) OR source_thread_id IN ({placeholders})",
                params + params,
            )
            self._conn.execute(f"DELETE FROM approvals WHERE thread_id IN ({placeholders})", params)
            self._conn.execute(f"DELETE FROM events WHERE thread_id IN ({placeholders})", params)
            self._conn.execute(f"DELETE FROM turns WHERE thread_id IN ({placeholders})", params)
            self._conn.execute(f"DELETE FROM threads WHERE thread_id IN ({placeholders})", params)
            self._conn.commit()

    def _row_to_thread(self, row: sqlite3.Row) -> ThreadRecord:
        return ThreadRecord(
            threadId=row["thread_id"],
            title=row["title"],
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
            parentThreadId=row["parent_thread_id"],
            forkedFromTurnId=row["forked_from_turn_id"],
            status=row["status"],
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def _row_to_turn(self, row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            turnId=row["turn_id"],
            threadId=row["thread_id"],
            idx=int(row["idx"]),
            userText=row["user_text"],
            status=row["status"],
            startedAt=row["started_at"],
            completedAt=row["completed_at"],
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def _row_to_event(self, row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            eventId=int(row["event_id"]),
            threadId=row["thread_id"],
            turnId=row["turn_id"],
            seq=int(row["seq"]),
            type=row["type"],
            ts=row["ts"],
            payload=_json_loads(row["payload_json"], {}),
        )

    def _row_to_approval(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            approvalId=row["approval_id"],
            threadId=row["thread_id"],
            turnId=row["turn_id"],
            itemId=row["item_id"],
            requestId=row["request_id"],
            requestMethod=row["request_method"],
            status=row["status"],
            details=_json_loads(row["details_json"], {}),
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
        )
