from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from backend.app.codex_manager import CodexSession
from backend.app.codex_rpc import JsonRpcError
from backend.app.db import Database
from backend.app.models import ThreadRecord, TurnRecord
from backend.app.turn_execution_service import TurnExecutionService
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeWs:
    def __init__(self) -> None:
        self.turn_updates: list[TurnRecord] = []
        self.thread_updates: list[ThreadRecord] = []

    async def emit_turn_updated(self, turn: TurnRecord) -> None:
        self.turn_updates.append(turn)

    async def emit_thread_updated(self, thread: ThreadRecord) -> None:
        self.thread_updates.append(thread)


class RpcStub:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict[str, object], int]] = []

    async def request_with_retry(self, method: str, params: dict[str, object], timeout_s: int = 60):
        self.calls.append((method, params, timeout_s))
        return await self._handler(method, params, timeout_s)

    async def close(self) -> None:
        return None


def make_turn_service(
    db: Database,
    ws: FakeWs,
    *,
    get_or_resume_session,
    ensure_turn_record,
) -> TurnExecutionService:
    return TurnExecutionService(
        db=db,
        ws=ws,  # type: ignore[arg-type]
        get_or_resume_session=get_or_resume_session,
        ensure_turn_record=ensure_turn_record,
        make_pending_turn=lambda idx, user_text: SimpleNamespace(idx=idx, user_text=user_text),
        monotonic_time=lambda: 55.0,
        now_iso=lambda: "2099-01-01T00:00:00Z",
    )


def test_start_turn_success_sets_active_turn_and_emits_update() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_execution.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now))

        async def rpc_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "turn/start":
                return {"turn": {"id": "turn-1", "status": "inProgress"}}
            raise AssertionError(f"Unexpected method {method}")

        session = CodexSession(process_key="s", rpc=RpcStub(rpc_handler), local_thread_id="thread-1", thread_id="thread-1")

        async def get_or_resume_session(thread_id: str):
            assert thread_id == "thread-1"
            return session

        def ensure_turn_record(thread_id: str, turn_id: str, status: str, pending) -> TurnRecord:
            turn = TurnRecord(
                turnId=turn_id,
                threadId=thread_id,
                idx=pending.idx,
                userText=pending.user_text,
                status="running" if status == "inProgress" else status,
                startedAt=now,
            )
            db.upsert_turn(turn)
            return turn

        service = make_turn_service(db, ws, get_or_resume_session=get_or_resume_session, ensure_turn_record=ensure_turn_record)

        turn = asyncio.run(service.start_turn("thread-1", "hello"))

        assert turn.turnId == "turn-1"
        assert session.active_turn_id == "turn-1"
        assert session.pending_turn is None
        assert session.last_used_monotonic == 55.0
        assert len(ws.turn_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_start_turn_rejects_when_turn_already_in_progress() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_execution.db")
    ws = FakeWs()
    try:
        async def rpc_handler(_method: str, _params: dict[str, object], _timeout_s: int):
            raise AssertionError("RPC should not be called")

        session = CodexSession(process_key="s", rpc=RpcStub(rpc_handler), thread_id="thread-1")
        session.active_turn_id = "turn-running"

        async def get_or_resume_session(_thread_id: str):
            return session

        service = make_turn_service(
            db,
            ws,
            get_or_resume_session=get_or_resume_session,
            ensure_turn_record=lambda *_args, **_kwargs: TurnRecord(
                turnId="unused",
                threadId="thread-1",
                idx=1,
                userText="",
                status="running",
                startedAt=utc_now(),
            ),
        )

        try:
            asyncio.run(service.start_turn("thread-1", "hello"))
            raise AssertionError("Expected turn_in_progress")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["error"]["code"] == "turn_in_progress"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_start_turn_rpc_error_clears_pending_and_returns_http_502() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_execution.db")
    ws = FakeWs()
    try:
        async def rpc_handler(_method: str, _params: dict[str, object], _timeout_s: int):
            raise JsonRpcError(-32000, "boom", {"x": 1})

        session = CodexSession(process_key="s", rpc=RpcStub(rpc_handler), thread_id="thread-1")

        async def get_or_resume_session(_thread_id: str):
            return session

        service = make_turn_service(
            db,
            ws,
            get_or_resume_session=get_or_resume_session,
            ensure_turn_record=lambda *_args, **_kwargs: TurnRecord(
                turnId="unused",
                threadId="thread-1",
                idx=1,
                userText="",
                status="running",
                startedAt=utc_now(),
            ),
        )

        try:
            asyncio.run(service.start_turn("thread-1", "hello"))
            raise AssertionError("Expected codex_rpc_error")
        except HTTPException as exc:
            assert exc.status_code == 502
            assert exc.detail["error"]["code"] == "codex_rpc_error"
        assert session.pending_turn is None
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_interrupt_turn_marks_turn_interrupted_and_updates_thread_idle() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_execution.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="t", createdAt=now, updatedAt=now, status="running"))
        db.upsert_turn(TurnRecord(turnId="turn-1", threadId="thread-1", idx=1, userText="prompt", status="running", startedAt=now))

        async def rpc_handler(method: str, _params: dict[str, object], _timeout_s: int):
            if method == "turn/interrupt":
                return {}
            raise AssertionError(f"Unexpected method {method}")

        session = CodexSession(process_key="s", rpc=RpcStub(rpc_handler), local_thread_id="thread-1", thread_id="thread-1")
        session.active_turn_id = "turn-1"
        session.pending_turn = SimpleNamespace(idx=2, user_text="queued")

        async def get_or_resume_session(_thread_id: str):
            return session

        service = make_turn_service(
            db,
            ws,
            get_or_resume_session=get_or_resume_session,
            ensure_turn_record=lambda *_args, **_kwargs: TurnRecord(
                turnId="unused",
                threadId="thread-1",
                idx=1,
                userText="",
                status="running",
                startedAt=now,
            ),
        )

        turn = asyncio.run(service.interrupt_turn("thread-1"))

        assert turn.turnId == "turn-1"
        assert turn.status == "interrupted"
        assert session.active_turn_id is None
        assert session.pending_turn is None
        stored_turn = db.get_turn("thread-1", "turn-1")
        assert stored_turn is not None
        assert stored_turn.status == "interrupted"
        assert stored_turn.metadata["interruptedByUser"] is True
        thread = db.get_thread("thread-1")
        assert thread is not None
        assert thread.status == "idle"
        assert len(ws.turn_updates) == 1
        assert len(ws.thread_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_interrupt_turn_rejects_when_no_running_turn_exists() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turn_execution.db")
    ws = FakeWs()
    try:
        async def rpc_handler(_method: str, _params: dict[str, object], _timeout_s: int):
            raise AssertionError("RPC should not be called")

        session = CodexSession(process_key="s", rpc=RpcStub(rpc_handler), thread_id="thread-1")

        async def get_or_resume_session(_thread_id: str):
            return session

        service = make_turn_service(
            db,
            ws,
            get_or_resume_session=get_or_resume_session,
            ensure_turn_record=lambda *_args, **_kwargs: TurnRecord(
                turnId="unused",
                threadId="thread-1",
                idx=1,
                userText="",
                status="running",
                startedAt=utc_now(),
            ),
        )

        try:
            asyncio.run(service.interrupt_turn("thread-1"))
            raise AssertionError("Expected turn_not_running")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["error"]["code"] == "turn_not_running"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
