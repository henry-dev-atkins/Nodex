from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from backend.app.codex_manager import CodexSession
from backend.app.db import Database
from backend.app.maintenance_service import MaintenanceService
from backend.app.models import ImportPreviewRecord, ThreadRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeRpc:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def make_session(process_key: str, thread_id: str, last_used: float) -> CodexSession:
    return CodexSession(
        process_key=process_key,
        rpc=FakeRpc(),
        local_thread_id=thread_id,
        thread_id=thread_id,
        last_used_monotonic=last_used,
    )


def test_retire_session_marks_intentional_removes_mapping_and_closes_rpc() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "maintenance.db")
    try:
        session = make_session("s1", "thread-1", 10.0)
        sessions = {"thread-1": session}
        service = MaintenanceService(
            db,
            sessions=sessions,
            session_lock=asyncio.Lock(),
            session_idle_ttl_s=600,
        )

        asyncio.run(service.retire_session(session))

        assert session.intentional_close is True
        assert "thread-1" not in sessions
        assert session.rpc.closed == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_evict_idle_sessions_retires_only_idle_sessions_older_than_cutoff() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "maintenance.db")
    try:
        old_idle = make_session("old-idle", "thread-old-idle", 10.0)
        new_idle = make_session("new-idle", "thread-new-idle", 95.0)
        old_busy = make_session("old-busy", "thread-old-busy", 5.0)
        old_busy.active_turn_id = "turn-running"
        sessions = {
            old_idle.local_thread_id: old_idle,
            new_idle.local_thread_id: new_idle,
            old_busy.local_thread_id: old_busy,
        }
        service = MaintenanceService(
            db,
            sessions=sessions,
            session_lock=asyncio.Lock(),
            session_idle_ttl_s=20,
        )

        asyncio.run(service.evict_idle_sessions(now_monotonic=100.0))

        assert "thread-old-idle" not in sessions
        assert "thread-new-idle" in sessions
        assert "thread-old-busy" in sessions
        assert old_idle.rpc.closed == 1
        assert new_idle.rpc.closed == 0
        assert old_busy.rpc.closed == 0
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_close_sessions_clears_mapping_and_closes_all_rpcs() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "maintenance.db")
    try:
        session_a = make_session("a", "thread-a", 10.0)
        session_b = make_session("b", "thread-b", 20.0)
        sessions = {
            session_a.local_thread_id: session_a,
            session_b.local_thread_id: session_b,
        }
        service = MaintenanceService(
            db,
            sessions=sessions,
            session_lock=asyncio.Lock(),
            session_idle_ttl_s=600,
        )

        asyncio.run(service.close_sessions())

        assert sessions == {}
        assert session_a.rpc.closed == 1
        assert session_b.rpc.closed == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_housekeeping_step_deletes_expired_previews_and_evicts_idle_sessions() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "maintenance.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now))
        preview = ImportPreviewRecord(
            previewId="preview-1",
            destThreadId="dest",
            destTurnId=None,
            sourceThreadId="source",
            sourceAnchorTurnId="source-turn-1",
            sourceNodes=[{"threadId": "source", "turnId": "source-turn-1"}],
            mergeMode="verbose",
            suspectedSecrets=[],
            transferBlob="blob",
            expiresAt="2000-01-01T00:00:00Z",
        )
        db.save_import_preview(preview)
        session = make_session("s", "thread-old", 10.0)
        sessions = {session.local_thread_id: session}
        service = MaintenanceService(
            db,
            sessions=sessions,
            session_lock=asyncio.Lock(),
            session_idle_ttl_s=20,
        )

        asyncio.run(service.housekeeping_step(now_iso="2099-01-01T00:00:00Z", now_monotonic=100.0))

        assert db.get_import_preview("preview-1") is None
        assert "thread-old" not in sessions
        assert session.rpc.closed == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
