from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from backend.app.codex_manager import CodexSession
from backend.app.db import Database
from backend.app.models import ThreadRecord
from backend.app.session_recovery import SessionRecoveryService
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

    async def emit_thread_updated(self, thread: ThreadRecord) -> None:
        self.thread_updates.append(thread)


class NoopRpc:
    async def close(self) -> None:
        return None


class ResumeRpc:
    def __init__(self, response: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.response = response or {"thread": {"id": "remote-thread"}}
        self.error = error

    async def request_with_retry(self, method: str, params: dict[str, object], timeout_s: int = 60):
        self.calls.append((method, params, timeout_s))
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        return None


def test_handle_exit_intentional_close_only_removes_session() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "recovery.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now, status="idle"))
        session = CodexSession(process_key="old", rpc=NoopRpc(), local_thread_id="thread-1", thread_id="thread-1")
        session.intentional_close = True
        sessions: dict[str, CodexSession] = {"thread-1": session}
        session_lock = asyncio.Lock()

        async def spawn_session() -> CodexSession:
            raise AssertionError("spawn_session should not be called for intentional close")

        service = SessionRecoveryService(
            db,
            ws,  # type: ignore[arg-type]
            sessions=sessions,
            session_lock=session_lock,
            spawn_session=spawn_session,
            thread_resume_params=lambda thread_id: {"threadId": thread_id},
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            monotonic_time=lambda: 0.0,
        )

        asyncio.run(service.handle_exit(session, 7, stopping=False))

        assert "thread-1" not in sessions
        assert ws.thread_updates == []
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_exit_restart_success_updates_session_and_thread_status() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "recovery.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="local-thread", title="t", createdAt=now, updatedAt=now, status="running"))
        old_session = CodexSession(process_key="old", rpc=NoopRpc(), local_thread_id="local-thread", thread_id="remote-thread")
        sessions: dict[str, CodexSession] = {"local-thread": old_session}
        session_lock = asyncio.Lock()
        resumed_rpc = ResumeRpc(response={"thread": {"id": "remote-thread", "createdAt": 0, "updatedAt": 0, "turns": []}})
        resumed_session = CodexSession(process_key="new", rpc=resumed_rpc)
        calls = {"sync": 0, "update": 0, "thread_resume_params": ""}

        async def spawn_session() -> CodexSession:
            return resumed_session

        def thread_resume_params(thread_id: str) -> dict[str, object]:
            calls["thread_resume_params"] = thread_id
            return {"threadId": thread_id, "persistExtendedHistory": True}

        def sync_thread_snapshot(_thread: dict[str, object]) -> ThreadRecord:
            calls["sync"] += 1
            thread = db.get_thread("local-thread")
            assert thread is not None
            return thread

        def update_local_thread_from_codex(local_id: str, _thread: dict[str, object]) -> ThreadRecord:
            calls["update"] += 1
            assert local_id == "local-thread"
            thread = db.get_thread("local-thread")
            assert thread is not None
            return thread

        service = SessionRecoveryService(
            db,
            ws,  # type: ignore[arg-type]
            sessions=sessions,
            session_lock=session_lock,
            spawn_session=spawn_session,
            thread_resume_params=thread_resume_params,
            sync_thread_snapshot=sync_thread_snapshot,
            update_local_thread_from_codex=update_local_thread_from_codex,
            monotonic_time=lambda: 123.0,
        )

        asyncio.run(service.handle_exit(old_session, 11, stopping=False))

        assert old_session.restart_attempted is True
        assert sessions["local-thread"] is resumed_session
        assert resumed_session.local_thread_id == "local-thread"
        assert resumed_session.thread_id == "remote-thread"
        assert resumed_session.restart_attempted is True
        assert resumed_session.last_used_monotonic == 123.0
        assert calls["thread_resume_params"] == "remote-thread"
        assert calls["update"] == 1
        assert calls["sync"] == 0
        assert resumed_rpc.calls == [
            ("thread/resume", {"threadId": "remote-thread", "persistExtendedHistory": True}, 60)
        ]
        thread = db.get_thread("local-thread")
        assert thread is not None
        assert thread.status == "idle"
        assert thread.metadata["lastRestartExitCode"] == 11
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_exit_restart_failure_marks_thread_dead_with_error() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "recovery.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now, status="running"))
        old_session = CodexSession(process_key="old", rpc=NoopRpc(), local_thread_id="thread-1", thread_id="thread-1")
        sessions: dict[str, CodexSession] = {"thread-1": old_session}
        session_lock = asyncio.Lock()

        async def spawn_session() -> CodexSession:
            return CodexSession(process_key="new", rpc=ResumeRpc(error=RuntimeError("resume exploded")))

        service = SessionRecoveryService(
            db,
            ws,  # type: ignore[arg-type]
            sessions=sessions,
            session_lock=session_lock,
            spawn_session=spawn_session,
            thread_resume_params=lambda thread_id: {"threadId": thread_id},
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            monotonic_time=lambda: 0.0,
        )

        asyncio.run(service.handle_exit(old_session, 22, stopping=False))

        assert "thread-1" not in sessions
        thread = db.get_thread("thread-1")
        assert thread is not None
        assert thread.status == "dead"
        assert thread.metadata["lastExitCode"] == 22
        assert "resume exploded" in thread.metadata["restartError"]
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_exit_after_restart_attempt_marks_dead_without_retry() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "recovery.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now, status="running"))
        session = CodexSession(process_key="old", rpc=NoopRpc(), local_thread_id="thread-1", thread_id="thread-1")
        session.restart_attempted = True
        sessions: dict[str, CodexSession] = {"thread-1": session}
        session_lock = asyncio.Lock()

        async def spawn_session() -> CodexSession:
            raise AssertionError("spawn_session should not be called when restart_attempted is already true")

        service = SessionRecoveryService(
            db,
            ws,  # type: ignore[arg-type]
            sessions=sessions,
            session_lock=session_lock,
            spawn_session=spawn_session,
            thread_resume_params=lambda thread_id: {"threadId": thread_id},
            sync_thread_snapshot=lambda _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            monotonic_time=lambda: 0.0,
        )

        asyncio.run(service.handle_exit(session, 9, stopping=False))

        assert "thread-1" not in sessions
        thread = db.get_thread("thread-1")
        assert thread is not None
        assert thread.status == "dead"
        assert thread.metadata["lastExitCode"] == 9
        assert "restartError" not in thread.metadata
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
