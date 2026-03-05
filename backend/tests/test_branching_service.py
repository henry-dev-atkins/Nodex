from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.branching_service import BranchingService
from backend.app.codex_manager import CodexSession
from backend.app.db import Database
from backend.app.models import ThreadRecord, TurnRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeWs:
    def __init__(self) -> None:
        self.forked: list[tuple[ThreadRecord, list[TurnRecord] | None]] = []

    async def emit_thread_forked(self, thread: ThreadRecord, turns: list[TurnRecord] | None = None) -> None:
        self.forked.append((thread, turns))


class RpcStub:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict[str, object], int]] = []

    async def request_with_retry(self, method: str, params: dict[str, object], timeout_s: int = 60):
        self.calls.append((method, params, timeout_s))
        return await self._handler(method, params, timeout_s)

    async def close(self) -> None:
        return None


def make_branching_service(
    db: Database,
    ws: FakeWs,
    sessions: dict[str, CodexSession],
    session_lock: asyncio.Lock,
    *,
    get_or_resume_session,
    spawn_session,
    retire_session,
    thread_resume_params,
    remote_thread_id,
    sync_thread_snapshot,
    lineage_turn_snapshots,
    history_from_turn_snapshots,
) -> BranchingService:
    return BranchingService(
        db=db,
        ws=ws,  # type: ignore[arg-type]
        sessions=sessions,
        session_lock=session_lock,
        get_or_resume_session=get_or_resume_session,
        spawn_session=spawn_session,
        retire_session=retire_session,
        thread_resume_params=thread_resume_params,
        remote_thread_id=remote_thread_id,
        sync_thread_snapshot=sync_thread_snapshot,
        lineage_turn_snapshots=lineage_turn_snapshots,
        history_from_turn_snapshots=history_from_turn_snapshots,
        monotonic_time=lambda: 100.0,
    )


def test_branch_from_turn_rejects_when_history_is_unavailable() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "branching.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="root",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
            )
        )

        async def get_or_resume_session(_thread_id: str):
            raise AssertionError("Not expected for branch_from_turn")

        async def spawn_session():
            raise AssertionError("spawn_session should not run when history is unavailable")

        async def retire_session(_session) -> None:
            return None

        service = make_branching_service(
            db,
            ws,
            sessions,
            session_lock,
            get_or_resume_session=get_or_resume_session,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            remote_thread_id=lambda thread: thread.threadId,
            sync_thread_snapshot=lambda _thread, _parent, _forked, _title: ThreadRecord(
                threadId="unused",
                title="unused",
                createdAt=now,
                updatedAt=now,
            ),
            lineage_turn_snapshots=lambda _thread_id, _upto, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
        )

        try:
            asyncio.run(service.branch_from_turn("root", "turn-1"))
            raise AssertionError("Expected history_unavailable error")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["error"]["code"] == "history_unavailable"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_branch_from_turn_reads_thread_snapshot_when_resume_is_empty() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "branching.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="root",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
            )
        )

        async def child_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/resume":
                return {"thread": {"id": "child-thread", "createdAt": 0, "updatedAt": 0, "turns": []}}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "child-thread",
                        "createdAt": 0,
                        "updatedAt": 0,
                        "turns": [{"id": "child-turn-1", "status": "completed", "items": []}],
                    }
                }
            raise AssertionError(f"Unexpected method {method}")

        child_session = CodexSession(process_key="child", rpc=RpcStub(child_handler))
        retired: list[str] = []

        async def get_or_resume_session(_thread_id: str):
            raise AssertionError("Not expected for branch_from_turn")

        async def spawn_session():
            return child_session

        async def retire_session(session) -> None:
            retired.append(session.process_key)

        def sync_thread_snapshot(thread: dict[str, object], parent: str | None, forked: str | None, title: str | None) -> ThreadRecord:
            assert parent == "root"
            assert forked == "turn-1"
            record = ThreadRecord(
                threadId=str(thread["id"]),
                title=title or "Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId=parent,
                forkedFromTurnId=forked,
            )
            db.upsert_thread(record)
            db.upsert_turn(
                TurnRecord(
                    turnId="child-turn-1",
                    threadId=record.threadId,
                    idx=1,
                    userText="child",
                    status="completed",
                    startedAt=now,
                )
            )
            return record

        service = make_branching_service(
            db,
            ws,
            sessions,
            session_lock,
            get_or_resume_session=get_or_resume_session,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            remote_thread_id=lambda thread: thread.threadId,
            sync_thread_snapshot=sync_thread_snapshot,
            lineage_turn_snapshots=lambda _thread_id, _upto, _include_error: [{"id": "turn-1", "items": []}],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [{"type": "message", "role": "user", "content": []}],
        )

        result = asyncio.run(service.branch_from_turn("root", "turn-1"))

        assert result.threadId == "child-thread"
        assert result.parentThreadId == "root"
        assert sessions["child-thread"] is child_session
        assert retired == []
        assert any(call[0] == "thread/read" for call in child_session.rpc.calls)
        assert len(ws.forked) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_branch_from_turn_drops_replayed_snapshot_when_resume_and_read_are_empty() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "branching.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="root",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
            )
        )

        async def child_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method in {"thread/resume", "thread/read"}:
                return {"thread": {"id": "child-thread", "createdAt": 0, "updatedAt": 0, "turns": []}}
            raise AssertionError(f"Unexpected method {method}")

        child_session = CodexSession(process_key="child", rpc=RpcStub(child_handler))
        retired: list[str] = []

        async def get_or_resume_session(_thread_id: str):
            raise AssertionError("Not expected for branch_from_turn")

        async def spawn_session():
            return child_session

        async def retire_session(session) -> None:
            retired.append(session.process_key)

        def sync_thread_snapshot(thread: dict[str, object], parent: str | None, forked: str | None, title: str | None) -> ThreadRecord:
            record = ThreadRecord(
                threadId="child-thread",
                title=title or "Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId=parent,
                forkedFromTurnId=forked,
            )
            db.upsert_thread(record)
            for idx, turn_payload in enumerate(thread.get("turns", []), start=1):
                db.upsert_turn(
                    TurnRecord(
                        turnId=str(turn_payload.get("id", f"turn-{idx}")),
                        threadId=record.threadId,
                        idx=idx,
                        userText="synthesized",
                        status=str(turn_payload.get("status", "completed")),
                        startedAt=now,
                        metadata={"items": turn_payload.get("items", [])},
                    )
                )
            return record

        service = make_branching_service(
            db,
            ws,
            sessions,
            session_lock,
            get_or_resume_session=get_or_resume_session,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            remote_thread_id=lambda thread: thread.threadId,
            sync_thread_snapshot=sync_thread_snapshot,
            lineage_turn_snapshots=lambda _thread_id, _upto, _include_error: [{"id": "turn-1", "items": []}],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [{"type": "message", "role": "user", "content": []}],
        )

        result = asyncio.run(service.branch_from_turn("root", "turn-1"))
        assert result.threadId == "child-thread"
        assert retired == []
        assert sessions["child-thread"] is child_session
        stored_turns = db.list_turns("child-thread")
        assert len(stored_turns) == 0
        assert any(call[0] == "thread/read" for call in child_session.rpc.calls)
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_branch_from_turn_allows_empty_snapshot_when_history_exists_without_lineage_turns() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "branching.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="root",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
            )
        )

        async def child_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method in {"thread/resume", "thread/read"}:
                return {"thread": {"id": "child-thread", "createdAt": 0, "updatedAt": 0, "turns": []}}
            raise AssertionError(f"Unexpected method {method}")

        child_session = CodexSession(process_key="child", rpc=RpcStub(child_handler))
        retired: list[str] = []

        async def get_or_resume_session(_thread_id: str):
            raise AssertionError("Not expected for branch_from_turn")

        async def spawn_session():
            return child_session

        async def retire_session(session) -> None:
            retired.append(session.process_key)

        def sync_thread_snapshot(thread: dict[str, object], parent: str | None, forked: str | None, title: str | None) -> ThreadRecord:
            record = ThreadRecord(
                threadId=str(thread["id"]),
                title=title or "Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId=parent,
                forkedFromTurnId=forked,
            )
            db.upsert_thread(record)
            return record

        service = make_branching_service(
            db,
            ws,
            sessions,
            session_lock,
            get_or_resume_session=get_or_resume_session,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            remote_thread_id=lambda thread: thread.threadId,
            sync_thread_snapshot=sync_thread_snapshot,
            lineage_turn_snapshots=lambda _thread_id, _upto, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [{"type": "message", "role": "user", "content": []}],
        )

        result = asyncio.run(service.branch_from_turn("root", "turn-1"))
        assert result.threadId == "child-thread"
        assert result.parentThreadId == "root"
        assert result.forkedFromTurnId == "turn-1"
        assert retired == []
        assert sessions["child-thread"] is child_session
        assert any(call[0] == "thread/read" for call in child_session.rpc.calls)
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_fork_thread_registers_child_session_and_emits_fork_event() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "branching.db")
    ws = FakeWs()
    sessions: dict[str, CodexSession] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_turn(TurnRecord(turnId="turn-1", threadId="root", idx=1, userText="prompt", status="completed", startedAt=now))

        async def parent_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/fork":
                return {"thread": {"id": "child-thread", "createdAt": 0, "updatedAt": 0, "turns": []}}
            raise AssertionError(f"Unexpected method {method}")

        async def child_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "thread/resume":
                return {"thread": {"id": "child-thread", "createdAt": 0, "updatedAt": 0, "turns": []}}
            raise AssertionError(f"Unexpected method {method}")

        parent_session = CodexSession(process_key="parent", rpc=RpcStub(parent_handler), local_thread_id="root", thread_id="root")
        child_session = CodexSession(process_key="child", rpc=RpcStub(child_handler))

        async def get_or_resume_session(thread_id: str):
            assert thread_id == "root"
            return parent_session

        async def spawn_session():
            return child_session

        async def retire_session(_session) -> None:
            return None

        def sync_thread_snapshot(thread: dict[str, object], parent: str | None, forked: str | None, title: str | None) -> ThreadRecord:
            record = ThreadRecord(
                threadId=str(thread["id"]),
                title=title or "Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId=parent,
                forkedFromTurnId=forked,
            )
            db.upsert_thread(record)
            return record

        service = make_branching_service(
            db,
            ws,
            sessions,
            session_lock,
            get_or_resume_session=get_or_resume_session,
            spawn_session=spawn_session,
            retire_session=retire_session,
            thread_resume_params=lambda thread_id, history: {"threadId": thread_id, "history": history},
            remote_thread_id=lambda thread: thread.threadId,
            sync_thread_snapshot=sync_thread_snapshot,
            lineage_turn_snapshots=lambda _thread_id, _upto, _include_error: [],
            history_from_turn_snapshots=lambda _turns, _include_tool_calls: [],
        )

        result = asyncio.run(service.fork_thread("root", title="Forked"))

        assert result.threadId == "child-thread"
        assert result.parentThreadId == "root"
        assert result.forkedFromTurnId == "turn-1"
        assert sessions["child-thread"] is child_session
        assert len(ws.forked) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
