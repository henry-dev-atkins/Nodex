from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from backend.app.db import Database
from backend.app.models import TurnRecord
from backend.app.turn_record_service import TurnRecordService
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class Pending:
    def __init__(self, idx: int, user_text: str) -> None:
        self.idx = idx
        self.user_text = user_text


def test_ensure_turn_record_creates_new_turn() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_records.db")
    try:
        service = TurnRecordService(
            db,
            normalize_turn_status=lambda status, _fallback: "running" if status == "inProgress" else str(status),
            now_iso=lambda: "2099-01-01T00:00:00Z",
        )

        turn = service.ensure_turn_record("thread-1", "turn-1", "inProgress", Pending(3, "hello"))

        assert turn.threadId == "thread-1"
        assert turn.turnId == "turn-1"
        assert turn.idx == 3
        assert turn.userText == "hello"
        assert turn.status == "running"
        assert turn.startedAt == "2099-01-01T00:00:00Z"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_ensure_turn_record_preserves_existing_fields() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_records.db")
    try:
        now = utc_now()
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-1",
                idx=1,
                userText="original",
                status="completed",
                startedAt=now,
                completedAt=now,
                metadata={"k": "v"},
            )
        )
        service = TurnRecordService(
            db,
            normalize_turn_status=lambda status, fallback: fallback if status == "unknown" else str(status),
            now_iso=lambda: "2099-01-01T00:00:00Z",
        )

        turn = service.ensure_turn_record("thread-1", "turn-1", "unknown", Pending(5, "new"))

        assert turn.idx == 1
        assert turn.userText == "original"
        assert turn.status == "completed"
        assert turn.startedAt == now
        assert turn.completedAt == now
        assert turn.metadata == {"k": "v"}
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
