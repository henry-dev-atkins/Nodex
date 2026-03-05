from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.db import Database
from backend.app.models import ThreadRecord, TurnRecord
from backend.app.response_history import ResponseHistoryProjector
from backend.app.turn_history import TurnHistoryService
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


def test_response_items_omit_tool_calls_when_disabled() -> None:
    projector = ResponseHistoryProjector()

    history = projector.response_items_from_thread_items(
        [
            {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
            {"type": "commandExecution", "id": "cmd-1", "status": "completed", "command": "python -V"},
            {"type": "webSearch", "query": "latest codex release"},
            {"type": "agentMessage", "text": "world"},
        ],
        include_tool_calls=False,
    )

    assert history == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "world"}]},
    ]


def test_sanitize_local_shell_action_omits_null_fields() -> None:
    projector = ResponseHistoryProjector()

    action = projector.sanitize_local_shell_action(
        {
            "type": "commandExecution",
            "command": "python -V",
            "cwd": None,
            "env": None,
            "user": None,
        }
    )

    assert action == {"type": "exec", "command": ["python", "-V"]}


def test_build_response_history_raises_when_turn_missing() -> None:
    projector = ResponseHistoryProjector()

    try:
        projector.build_response_history({"turns": [{"id": "turn-1", "items": []}]}, "missing-turn")
        raise AssertionError("Expected build_response_history to reject unknown turn")
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail["error"]["code"] == "turn_not_found"


def test_items_from_turn_events_dedupes_by_item_id() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "history.db")
    projector = ResponseHistoryProjector()
    service = TurnHistoryService(db, projector)
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.append_event("thread-1", "turn-1", 1, "item/completed", {"item": {"id": "itm-1", "type": "agentMessage", "text": "first"}})
        db.append_event("thread-1", "turn-1", 2, "item/completed", {"item": {"id": "itm-1", "type": "agentMessage", "text": "final"}})

        items = service.items_from_turn_events("thread-1", "turn-1")

        assert items == [{"id": "itm-1", "type": "agentMessage", "text": "final"}]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_lineage_turn_snapshots_injects_user_message_fallback() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "history.db")
    projector = ResponseHistoryProjector()
    service = TurnHistoryService(db, projector)
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="Recover from prompt text",
                status="completed",
                startedAt=now,
                metadata={},
            )
        )
        db.append_event(
            "thread-1",
            "turn-1",
            1,
            "item/completed",
            {"item": {"id": "itm-1", "type": "agentMessage", "text": "answer"}},
        )

        snapshots = service.lineage_turn_snapshots("thread-1", upto_turn_id="turn-1", include_error_turns=False)

        assert len(snapshots) == 1
        assert snapshots[0]["id"] == "turn-1"
        assert snapshots[0]["items"][0]["type"] == "userMessage"
        assert snapshots[0]["items"][0]["content"][0]["text"] == "Recover from prompt text"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_persist_turn_items_from_events_updates_metadata() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "history.db")
    projector = ResponseHistoryProjector()
    service = TurnHistoryService(db, projector)
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
                completedAt=now,
                metadata={},
            )
        )
        db.append_event(
            "thread-1",
            "turn-1",
            1,
            "item/completed",
            {"item": {"id": "itm-user", "type": "userMessage", "content": [{"type": "text", "text": "prompt"}]}},
        )
        turn = db.get_turn("thread-1", "turn-1")
        assert turn is not None

        persisted = service.persist_turn_items_from_events(turn)

        item_types = [item.get("type") for item in persisted.metadata.get("items", [])]
        assert item_types == ["userMessage"]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
