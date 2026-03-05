from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from backend.app.db import Database
from backend.app.models import ThreadRecord, TurnRecord
from backend.app.notification_effects import NotificationEffectsService
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeWs:
    def __init__(self) -> None:
        self.thread_updates: list[ThreadRecord] = []
        self.turn_updates: list[TurnRecord] = []

    async def emit_thread_updated(self, thread: ThreadRecord) -> None:
        self.thread_updates.append(thread)

    async def emit_turn_updated(self, turn: TurnRecord) -> None:
        self.turn_updates.append(turn)


class SessionStub:
    def __init__(self, local_thread_id: str | None, thread_id: str | None) -> None:
        self.local_thread_id = local_thread_id
        self.thread_id = thread_id
        self.pending_turn = None
        self.active_turn_id = None


class PendingTurnStub:
    def __init__(self, idx: int, user_text: str) -> None:
        self.idx = idx
        self.user_text = user_text


def normalize_thread_status(status) -> str:
    if isinstance(status, dict) and status.get("type") == "active":
        return "running"
    if isinstance(status, str):
        return status
    return "idle"


def normalize_turn_status(status, fallback: str = "running") -> str:
    if status == "inProgress":
        return "running"
    if status == "failed":
        return "error"
    if isinstance(status, str):
        return status
    return fallback


def test_thread_started_uses_local_thread_update_when_local_and_remote_ids_differ() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "effects.db")
    ws = FakeWs()
    try:
        now = utc_now()
        local_thread = ThreadRecord(threadId="local-thread", title="Local", createdAt=now, updatedAt=now)
        db.upsert_thread(local_thread)
        calls = {"sync": 0, "update": 0}

        def sync_thread_snapshot(_thread: dict[str, object]) -> ThreadRecord:
            calls["sync"] += 1
            return local_thread

        def update_local_thread_from_codex(local_thread_id: str, _thread: dict[str, object]) -> ThreadRecord:
            calls["update"] += 1
            assert local_thread_id == "local-thread"
            return local_thread

        service = NotificationEffectsService(
            db,
            ws,  # type: ignore[arg-type]
            extract_thread_id=lambda payload: payload.get("threadId"),  # type: ignore[return-value]
            normalize_thread_status=normalize_thread_status,
            normalize_turn_status=normalize_turn_status,
            ensure_turn_record=lambda *args: TurnRecord(  # pragma: no cover - not used in this test
                turnId="unused",
                threadId="unused",
                idx=1,
                userText="",
                status="running",
                startedAt=now,
            ),
            persist_turn_items_from_events=lambda turn: turn,
            sync_thread_snapshot=sync_thread_snapshot,
            update_local_thread_from_codex=update_local_thread_from_codex,
            make_pending_turn=lambda idx, user_text: PendingTurnStub(idx, user_text),
        )
        session = SessionStub(local_thread_id="local-thread", thread_id="remote-thread")
        params = {"thread": {"id": "remote-thread"}}

        asyncio.run(service.apply(session, "thread/started", params))

        assert calls["update"] == 1
        assert calls["sync"] == 0
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_thread_status_changed_updates_db_and_emits() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "effects.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now, status="idle"))

        service = NotificationEffectsService(
            db,
            ws,  # type: ignore[arg-type]
            extract_thread_id=lambda payload: payload.get("threadId"),  # type: ignore[return-value]
            normalize_thread_status=normalize_thread_status,
            normalize_turn_status=normalize_turn_status,
            ensure_turn_record=lambda *args: TurnRecord(  # pragma: no cover - not used in this test
                turnId="unused",
                threadId="unused",
                idx=1,
                userText="",
                status="running",
                startedAt=now,
            ),
            persist_turn_items_from_events=lambda turn: turn,
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            make_pending_turn=lambda idx, user_text: PendingTurnStub(idx, user_text),
        )
        session = SessionStub(local_thread_id="thread-1", thread_id="thread-1")

        asyncio.run(service.apply(session, "thread/status/changed", {"status": {"type": "active"}}))

        thread = db.get_thread("thread-1")
        assert thread is not None
        assert thread.status == "running"
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_turn_completed_clears_session_state_and_emits_updated_turn() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "effects.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="prompt",
                status="running",
                startedAt=now,
            )
        )
        persisted_calls = {"count": 0}

        def ensure_turn_record(thread_id: str, turn_id: str, status: str, pending: PendingTurnStub) -> TurnRecord:
            turn = TurnRecord(
                turnId=turn_id,
                threadId=thread_id,
                idx=pending.idx,
                userText=pending.user_text,
                status=status,
                startedAt=now,
            )
            db.upsert_turn(turn)
            return turn

        def persist_turn_items_from_events(turn: TurnRecord) -> TurnRecord:
            persisted_calls["count"] += 1
            return turn

        service = NotificationEffectsService(
            db,
            ws,  # type: ignore[arg-type]
            extract_thread_id=lambda payload: payload.get("threadId"),  # type: ignore[return-value]
            normalize_thread_status=normalize_thread_status,
            normalize_turn_status=normalize_turn_status,
            ensure_turn_record=ensure_turn_record,
            persist_turn_items_from_events=persist_turn_items_from_events,
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            make_pending_turn=lambda idx, user_text: PendingTurnStub(idx, user_text),
        )
        session = SessionStub(local_thread_id="thread-1", thread_id="thread-1")
        session.active_turn_id = "turn-1"
        session.pending_turn = PendingTurnStub(idx=2, user_text="queued")

        asyncio.run(service.apply(session, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}))

        turn = db.get_turn("thread-1", "turn-1")
        assert turn is not None
        assert turn.status == "completed"
        assert session.active_turn_id is None
        assert session.pending_turn is None
        assert persisted_calls["count"] == 1
        assert len(ws.turn_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_error_turn_marks_turn_error_and_clears_session_state() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "effects.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="prompt",
                status="running",
                startedAt=now,
                metadata={},
            )
        )

        service = NotificationEffectsService(
            db,
            ws,  # type: ignore[arg-type]
            extract_thread_id=lambda payload: payload.get("threadId"),  # type: ignore[return-value]
            normalize_thread_status=normalize_thread_status,
            normalize_turn_status=normalize_turn_status,
            ensure_turn_record=lambda *args: db.get_turn("thread-1", "turn-1"),  # type: ignore[return-value]
            persist_turn_items_from_events=lambda turn: turn,
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            make_pending_turn=lambda idx, user_text: PendingTurnStub(idx, user_text),
        )
        session = SessionStub(local_thread_id="thread-1", thread_id="thread-1")
        session.active_turn_id = "turn-1"
        session.pending_turn = PendingTurnStub(idx=2, user_text="queued")

        asyncio.run(service.apply(session, "error", {"turnId": "turn-1", "error": {"message": "boom"}}))

        turn = db.get_turn("thread-1", "turn-1")
        assert turn is not None
        assert turn.status == "error"
        assert turn.metadata["error"] == {"message": "boom"}
        assert session.active_turn_id is None
        assert session.pending_turn is None
        assert len(ws.turn_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
