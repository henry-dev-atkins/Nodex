from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.codex_manager import CodexSession
from backend.app.db import Database
from backend.app.lifecycle_service import LifecycleService
from backend.app.models import ThreadRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeWs:
    def __init__(self) -> None:
        self.created_threads: list[ThreadRecord] = []

    async def emit_thread_created(self, thread: ThreadRecord) -> None:
        self.created_threads.append(thread)


class RpcStub:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict[str, object], int]] = []

    async def request_with_retry(self, method: str, params: dict[str, object], timeout_s: int = 60):
        self.calls.append((method, params, timeout_s))
        return await self._handler(method, params, timeout_s)

    async def close(self) -> None:
        return None


def make_lifecycle_service(
    db: Database,
    ws: FakeWs,
    sessions: dict[str, CodexSession],
    session_lock: asyncio.Lock,
    *,
    spawn_session,
    retire_session,
    thread_start_params,
    thread_resume_params,
    thread_record_from_codex,
    sync_thread_snapshot,
    update_local_thread_from_codex,
    remote_thread_id,
    lineage_turn_snapshots,
    history_from_turn_snapshots,
    monotonic_time,
) -> LifecycleService:
    return LifecycleService(
        db=db,
        ws=ws,  # type: ignore[arg-type]
        sessions=sessions,
        session_lock=session_lock,
        spawn_session=spawn_session,
        retire_session=retire_session,
        thread_start_params=thread_start_params,
        thread_resume_params=thread_resume_params,
        thread_record_from_codex=thread_record_from_codex,
        sync_thread_snapshot=sync_thread_snapshot,
        update_local_thread_from_codex=update_local_thread_from_codex,
        remote_thread_id=remote_thread_id,
        lineage_turn_snapshots=lineage_turn_snapshots,
        history_from_turn_snapshots=history_from_turn_snapshots,
        monotonic_time=monotonic_time,
    )


def test_start_thread_registers_session_persists_thread_and_emits_created_event() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "lifecycle.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()

        async def rpc_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/start":
                return {"thread": {"id": "thread-1", "createdAt": 0, "updatedAt": 0}}
            raise AssertionError(f"Unexpected method {method}")

        session = CodexSession(process_key="session-1", rpc=RpcStub(rpc_handler))

        async def spawn_session():
            return session

        async def retire_session(_session) -> None:
            return None

        def thread_record_from_codex(thread: dict[str, object], title: str | None) -> ThreadRecord:
            return ThreadRecord(
                threadId=str(thread["id"]),
                title=title,
                createdAt=now,
                updatedAt=now,
            )

        service = make_lifecycle_service(
            db,
            ws,
            sessions,
            session_lock,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_start_params=lambda: {"cwd": "C:/repo"},
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            thread_record_from_codex=thread_record_from_codex,
            sync_thread_snapshot=lambda _thread, _parent, _forked, _title: thread_record_from_codex({"id": "unused"}, None),
            update_local_thread_from_codex=lambda _local_id, _thread: thread_record_from_codex({"id": "unused"}, None),
            remote_thread_id=lambda thread: thread.threadId,
            lineage_turn_snapshots=lambda _thread_id, _upto_turn_id, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
            monotonic_time=lambda: 42.0,
        )

        result = asyncio.run(service.start_thread(title="New thread"))

        assert result.threadId == "thread-1"
        assert result.title == "New thread"
        assert sessions["thread-1"] is session
        assert session.local_thread_id == "thread-1"
        assert session.thread_id == "thread-1"
        assert session.last_used_monotonic == 42.0
        assert db.get_thread("thread-1") is not None
        assert len(ws.created_threads) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_get_or_resume_session_returns_existing_session_and_updates_last_used() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "lifecycle.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))
        existing = CodexSession(process_key="existing", rpc=RpcStub(lambda *_args, **_kwargs: asyncio.sleep(0)))
        existing.last_used_monotonic = 5.0
        sessions["thread-1"] = existing

        async def spawn_session():
            raise AssertionError("spawn_session should not be called for existing session")

        async def retire_session(_session) -> None:
            raise AssertionError("retire_session should not be called for healthy existing session")

        service = make_lifecycle_service(
            db,
            ws,
            sessions,
            session_lock,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_start_params=lambda: {},
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            thread_record_from_codex=lambda _thread, _title: db.get_thread("thread-1"),  # type: ignore[return-value]
            sync_thread_snapshot=lambda _thread, _parent, _forked, _title: db.get_thread("thread-1"),  # type: ignore[return-value]
            update_local_thread_from_codex=lambda _local_id, _thread: db.get_thread("thread-1"),  # type: ignore[return-value]
            remote_thread_id=lambda thread: thread.threadId,
            lineage_turn_snapshots=lambda _thread_id, _upto_turn_id, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
            monotonic_time=lambda: 99.0,
        )

        resumed = asyncio.run(service.get_or_resume_session("thread-1"))

        assert resumed is existing
        assert existing.last_used_monotonic == 99.0
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_get_or_resume_session_resumes_root_and_updates_local_mapping_when_remote_id_differs() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "lifecycle.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="local-thread", title="Local", createdAt=now, updatedAt=now))
        calls = {"sync": 0, "update": 0}

        async def rpc_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/resume":
                return {"thread": {"id": "remote-thread", "createdAt": 0, "updatedAt": 0}}
            raise AssertionError(f"Unexpected method {method}")

        session = CodexSession(process_key="spawned", rpc=RpcStub(rpc_handler))

        async def spawn_session():
            return session

        async def retire_session(_session) -> None:
            return None

        def sync_thread_snapshot(_thread: dict[str, object], _parent: str | None, _forked: str | None, _title: str | None) -> ThreadRecord:
            calls["sync"] += 1
            thread = db.get_thread("local-thread")
            assert thread is not None
            return thread

        def update_local_thread_from_codex(local_thread_id: str, _thread: dict[str, object]) -> ThreadRecord:
            calls["update"] += 1
            assert local_thread_id == "local-thread"
            thread = db.get_thread("local-thread")
            assert thread is not None
            return thread

        service = make_lifecycle_service(
            db,
            ws,
            sessions,
            session_lock,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_start_params=lambda: {},
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            thread_record_from_codex=lambda _thread, _title: db.get_thread("local-thread"),  # type: ignore[return-value]
            sync_thread_snapshot=sync_thread_snapshot,
            update_local_thread_from_codex=update_local_thread_from_codex,
            remote_thread_id=lambda _thread: "remote-parent-thread",
            lineage_turn_snapshots=lambda _thread_id, _upto_turn_id, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
            monotonic_time=lambda: 77.0,
        )

        resumed = asyncio.run(service.get_or_resume_session("local-thread"))

        assert resumed is session
        assert session.local_thread_id == "local-thread"
        assert session.thread_id == "remote-thread"
        assert session.last_used_monotonic == 77.0
        assert sessions["local-thread"] is session
        assert calls["update"] == 1
        assert calls["sync"] == 0
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_resume_child_session_from_db_requires_existing_parent() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "lifecycle.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        child = ThreadRecord(
            threadId="child",
            title="Child",
            createdAt=now,
            updatedAt=now,
            parentThreadId="missing-parent",
            forkedFromTurnId="turn-1",
        )
        db.upsert_thread(child)

        async def spawn_session():
            raise AssertionError("spawn_session should not be called when parent is missing")

        async def retire_session(_session) -> None:
            return None

        service = make_lifecycle_service(
            db,
            ws,
            sessions,
            session_lock,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_start_params=lambda: {},
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            thread_record_from_codex=lambda _thread, _title: child,
            sync_thread_snapshot=lambda _thread, _parent, _forked, _title: child,
            update_local_thread_from_codex=lambda _local_id, _thread: child,
            remote_thread_id=lambda thread: thread.threadId,
            lineage_turn_snapshots=lambda _thread_id, _upto_turn_id, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
            monotonic_time=lambda: 0.0,
        )

        try:
            asyncio.run(service.resume_child_session_from_db(child))
            raise AssertionError("Expected thread_unavailable when parent is missing")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["error"]["code"] == "thread_unavailable"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_resume_child_session_from_db_resumes_and_registers_session() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "lifecycle.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        parent = ThreadRecord(threadId="parent", title="Parent", createdAt=now, updatedAt=now)
        child = ThreadRecord(
            threadId="child",
            title="Child",
            createdAt=now,
            updatedAt=now,
            parentThreadId="parent",
            forkedFromTurnId="turn-1",
        )
        db.upsert_thread(parent)
        db.upsert_thread(child)
        calls = {"history": None, "remote": None, "update": 0}

        async def rpc_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/resume":
                return {"thread": {"id": "remote-child", "createdAt": 0, "updatedAt": 0}}
            raise AssertionError(f"Unexpected method {method}")

        session = CodexSession(process_key="spawned", rpc=RpcStub(rpc_handler))

        async def spawn_session():
            return session

        async def retire_session(_session) -> None:
            return None

        def thread_resume_params(thread_id: str, history: list[dict[str, object]] | None) -> dict[str, object]:
            calls["remote"] = thread_id
            calls["history"] = history
            return {"threadId": thread_id, "history": history}

        def update_local_thread_from_codex(local_thread_id: str, _thread: dict[str, object]) -> ThreadRecord:
            calls["update"] += 1
            assert local_thread_id == "child"
            stored = db.get_thread("child")
            assert stored is not None
            return stored

        service = make_lifecycle_service(
            db,
            ws,
            sessions,
            session_lock,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_start_params=lambda: {},
            thread_resume_params=thread_resume_params,
            thread_record_from_codex=lambda _thread, _title: child,
            sync_thread_snapshot=lambda _thread, _parent, _forked, _title: child,
            update_local_thread_from_codex=update_local_thread_from_codex,
            remote_thread_id=lambda thread: f"remote-{thread.threadId}",
            lineage_turn_snapshots=lambda _thread_id, _upto_turn_id, _include_error: [{"id": "turn-1", "items": []}],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [{"type": "message", "role": "user", "content": []}],
            monotonic_time=lambda: 123.0,
        )

        resumed = asyncio.run(service.resume_child_session_from_db(child))

        assert resumed is session
        assert session.local_thread_id == "child"
        assert session.thread_id == "remote-child"
        assert session.last_used_monotonic == 123.0
        assert sessions["child"] is session
        assert calls["remote"] == "remote-parent"
        assert calls["history"] == [{"type": "message", "role": "user", "content": []}]
        assert calls["update"] == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
