from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from backend.app.db import Database
from backend.app.thread_snapshot_service import ThreadSnapshotService
from backend.app.models import ThreadRecord, TurnRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


def test_status_normalization() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "snapshots.db")
    try:
        service = ThreadSnapshotService(db, extract_user_text_from_items=lambda _items: "", now_iso=utc_now)
        assert service.normalize_thread_status({"type": "active"}) == "running"
        assert service.normalize_thread_status({"type": "systemError"}) == "error"
        assert service.normalize_turn_status("inProgress") == "running"
        assert service.normalize_turn_status("failed") == "error"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_sync_thread_snapshot_preserves_existing_metadata_and_updates_items() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "snapshots.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-a", title="Thread", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-a",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
                metadata={"contextLinks": [{"sourceThreadId": "source", "sourceTurnId": "turn-source"}]},
            )
        )
        service = ThreadSnapshotService(
            db,
            extract_user_text_from_items=lambda items: "".join(
                part.get("text", "")
                for item in items
                if isinstance(item, dict)
                for part in item.get("content", [])
                if isinstance(part, dict)
            ),
            now_iso=utc_now,
        )

        service.sync_thread_snapshot(
            {
                "id": "thread-a",
                "createdAt": 0,
                "updatedAt": 0,
                "turns": [{"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "text": "answer"}]}],
            }
        )

        turn = db.get_turn("thread-a", "turn-1")
        assert turn is not None
        assert turn.metadata["contextLinks"] == [{"sourceThreadId": "source", "sourceTurnId": "turn-source"}]
        assert turn.metadata["items"] == [{"type": "agentMessage", "text": "answer"}]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_update_local_thread_from_codex_keeps_local_identity() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "snapshots.db")
    try:
        now = utc_now()
        db.upsert_thread(
            ThreadRecord(
                threadId="local-thread",
                title="Local",
                createdAt=now,
                updatedAt=now,
                parentThreadId="parent",
                forkedFromTurnId="turn-parent-1",
                status="idle",
                metadata={"remoteThreadId": "remote-old", "extra": "value"},
            )
        )
        service = ThreadSnapshotService(db, extract_user_text_from_items=lambda _items: "", now_iso=utc_now)

        updated = service.update_local_thread_from_codex(
            "local-thread",
            {
                "id": "remote-new",
                "name": "Renamed",
                "updatedAt": 1000,
                "status": {"type": "active"},
                "preview": "Preview",
            },
        )

        assert updated.threadId == "local-thread"
        assert updated.parentThreadId == "parent"
        assert updated.forkedFromTurnId == "turn-parent-1"
        assert updated.title == "Renamed"
        assert updated.status == "running"
        assert updated.metadata["remoteThreadId"] == "remote-new"
        assert updated.metadata["extra"] == "value"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_from_unix_uses_now_for_missing_values() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "snapshots.db")
    try:
        service = ThreadSnapshotService(
            db,
            extract_user_text_from_items=lambda _items: "",
            now_iso=lambda: "2099-01-01T00:00:00Z",
        )
        assert service.from_unix(None) == "2099-01-01T00:00:00Z"
        assert service.from_unix(0) == "1970-01-01T00:00:00Z"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
