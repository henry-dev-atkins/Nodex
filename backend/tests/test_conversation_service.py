from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.conversation_service import ConversationService
from backend.app.db import Database
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
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[str] = []

    async def emit_thread_deleted(self, thread_id: str, conversation_id: str) -> None:
        self.deleted.append((thread_id, conversation_id))

    async def emit_thread_updated(self, thread: ThreadRecord) -> None:
        self.updated.append(thread.threadId)


class FakeSession:
    def __init__(self, process_key: str) -> None:
        self.process_key = process_key


def test_rename_thread_validates_title_and_emits_update() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "conversations.db")
    ws = FakeWs()
    sessions: dict[str, FakeSession] = {}
    retired: list[str] = []
    lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="Original", createdAt=now, updatedAt=now))

        async def ensure_thread(thread_id: str) -> ThreadRecord:
            thread = db.get_thread(thread_id)
            if not thread:
                raise HTTPException(
                    status_code=404,
                    detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
                )
            return thread

        async def retire_session(session: FakeSession) -> None:
            retired.append(session.process_key)

        service = ConversationService(
            db,
            ws,  # type: ignore[arg-type]
            sessions,
            lock,
            ensure_thread=ensure_thread,
            retire_session=retire_session,
        )

        updated = asyncio.run(service.rename_thread("thread-1", "  Renamed  "))
        assert updated.title == "Renamed"
        assert ws.updated == ["thread-1"]
        assert retired == []

        try:
            asyncio.run(service.rename_thread("thread-1", "   "))
            raise AssertionError("Expected rename validation to fail")
        except HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail["error"]["code"] == "invalid_request"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_delete_branch_only_removes_selected_subtree() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "conversations.db")
    ws = FakeWs()
    lock = asyncio.Lock()
    retired: list[str] = []
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_thread(
            ThreadRecord(
                threadId="child-a",
                title="A",
                createdAt=now,
                updatedAt=now,
                parentThreadId="root",
                forkedFromTurnId="turn-1",
            )
        )
        db.upsert_thread(
            ThreadRecord(
                threadId="child-b",
                title="B",
                createdAt=now,
                updatedAt=now,
                parentThreadId="root",
                forkedFromTurnId="turn-1",
            )
        )
        db.upsert_thread(
            ThreadRecord(
                threadId="grandchild-a1",
                title="A1",
                createdAt=now,
                updatedAt=now,
                parentThreadId="child-a",
                forkedFromTurnId="turn-2",
            )
        )

        sessions: dict[str, FakeSession] = {
            "child-a": FakeSession("proc-a"),
            "grandchild-a1": FakeSession("proc-a1"),
        }

        async def ensure_thread(thread_id: str) -> ThreadRecord:
            thread = db.get_thread(thread_id)
            if not thread:
                raise HTTPException(
                    status_code=404,
                    detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
                )
            return thread

        async def retire_session(session: FakeSession) -> None:
            retired.append(session.process_key)

        service = ConversationService(
            db,
            ws,  # type: ignore[arg-type]
            sessions,
            lock,
            ensure_thread=ensure_thread,
            retire_session=retire_session,
        )

        result = asyncio.run(service.delete_branch("child-a"))

        assert result["conversationId"] == "root"
        assert set(result["deletedThreadIds"]) == {"child-a", "grandchild-a1"}
        assert db.get_thread("child-a") is None
        assert db.get_thread("grandchild-a1") is None
        assert db.get_thread("root") is not None
        assert db.get_thread("child-b") is not None
        assert set(retired) == {"proc-a", "proc-a1"}
        assert set(ws.deleted) == {("child-a", "root"), ("grandchild-a1", "root")}
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_delete_conversation_removes_full_tree() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "conversations.db")
    ws = FakeWs()
    lock = asyncio.Lock()
    retired: list[str] = []
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_thread(
            ThreadRecord(
                threadId="child",
                title="Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId="root",
                forkedFromTurnId="turn-1",
            )
        )
        sessions: dict[str, FakeSession] = {
            "root": FakeSession("proc-root"),
            "child": FakeSession("proc-child"),
        }

        async def ensure_thread(thread_id: str) -> ThreadRecord:
            thread = db.get_thread(thread_id)
            if not thread:
                raise HTTPException(
                    status_code=404,
                    detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
                )
            return thread

        async def retire_session(session: FakeSession) -> None:
            retired.append(session.process_key)

        service = ConversationService(
            db,
            ws,  # type: ignore[arg-type]
            sessions,
            lock,
            ensure_thread=ensure_thread,
            retire_session=retire_session,
        )

        result = asyncio.run(service.delete_conversation("child"))

        assert result["conversationId"] == "root"
        assert set(result["deletedThreadIds"]) == {"root", "child"}
        assert db.get_thread("root") is None
        assert db.get_thread("child") is None
        assert set(retired) == {"proc-root", "proc-child"}
        assert set(ws.deleted) == {("root", "root"), ("child", "root")}
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
