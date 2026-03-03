from __future__ import annotations

from pathlib import Path
import shutil
import uuid
import asyncio

from backend.app.codex_manager import ApprovalHandle, CodexManager, CodexSession
from backend.app.db import Database
from backend.app.models import ApprovalRecord, ThreadRecord, TurnRecord
from backend.app.settings import Settings
from backend.app.util import utc_now
from backend.app.ws import WebSocketHub


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


def make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        host="127.0.0.1",
        port=8787,
        codex_bin="codex",
        supported_codex_version_pattern=r"^0\.106\.",
        data_dir=data_dir,
        db_path=data_dir / "test.db",
        token_path=data_dir / "session_token.txt",
        schema_cache_dir=data_dir / "schema",
        frontend_dir=tmp_path / "frontend",
        workspace_dir=tmp_path,
        approval_policy="on-request",
        session_limit=4,
        session_idle_ttl_s=600,
        import_preview_ttl_s=900,
        launch_browser=False,
    )


def test_database_keeps_same_turn_id_across_threads() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "turns.db")
    try:
        shared_turn_id = "turn-001"
        db.upsert_turn(
            TurnRecord(
                turnId=shared_turn_id,
                threadId="thread-a",
                idx=1,
                userText="alpha",
                status="completed",
                startedAt=utc_now(),
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId=shared_turn_id,
                threadId="thread-b",
                idx=1,
                userText="beta",
                status="completed",
                startedAt=utc_now(),
            )
        )

        assert db.get_turn("thread-a", shared_turn_id).userText == "alpha"
        assert db.get_turn("thread-b", shared_turn_id).userText == "beta"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_build_transfer_blob_uses_source_thread_turn() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="source", title="Source", createdAt=now, updatedAt=now))
        db.upsert_thread(ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now))

        db.upsert_turn(
            TurnRecord(
                turnId="turn-shared",
                threadId="source",
                idx=1,
                userText="source prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId="turn-shared",
                threadId="dest",
                idx=1,
                userText="dest prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.append_event(
            "source",
            "turn-shared",
            1,
            "item/completed",
            {"item": {"type": "agentMessage", "text": "source answer"}},
        )

        blob = manager._build_transfer_blob("source", ["turn-shared"])

        assert "source prompt" in blob
        assert "source answer" in blob
        assert "dest prompt" not in blob
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_status_normalization() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        assert manager._normalize_thread_status({"type": "active"}) == "running"
        assert manager._normalize_thread_status({"type": "systemError"}) == "error"
        assert manager._normalize_turn_status("inProgress") == "running"
        assert manager._normalize_turn_status("failed") == "error"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


class FakeRpc:
    def __init__(self) -> None:
        self.responses: list[tuple[object, object, object]] = []

    async def send_response(self, request_id, result=None, error=None) -> None:
        self.responses.append((request_id, result, error))


def test_approval_response_preserves_numeric_jsonrpc_id() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        now = utc_now()
        approval = ApprovalRecord(
            approvalId="approval-1",
            threadId="thread-a",
            turnId="turn-a",
            itemId="item-a",
            requestId="0",
            requestMethod="item/fileChange/requestApproval",
            status="pending",
            details={"threadId": "thread-a", "turnId": "turn-a", "itemId": "item-a"},
            createdAt=now,
            updatedAt=now,
        )
        db.upsert_approval(approval)
        rpc = FakeRpc()
        session = CodexSession(process_key="proc-1", rpc=rpc, thread_id="thread-a")
        session.pending_approvals["approval-1"] = ApprovalHandle(
            request_id=0,
            method="item/fileChange/requestApproval",
            params=approval.details,
        )
        manager.sessions["thread-a"] = session

        result = asyncio.run(manager.respond_approval("approval-1", "approve"))

        assert result.status == "approve"
        assert rpc.responses == [(0, {"decision": "accept"}, None)]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
