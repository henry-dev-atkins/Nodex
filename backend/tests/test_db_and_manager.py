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
        assert "Summary:" in blob
        assert "Decision:" in blob
        assert "Result:" in blob
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


def test_commit_import_preview_links_source_turns_on_destination_turn() -> None:
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
                turnId="turn-source-1",
                threadId="source",
                idx=1,
                userText="source prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId="turn-source-2",
                threadId="source",
                idx=2,
                userText="follow-up prompt",
                status="completed",
                startedAt=now,
            )
        )

        preview = asyncio.run(manager.create_import_preview("source", ["turn-source-1", "turn-source-2"], "dest"))

        async def fake_start_turn(thread_id: str, text: str) -> TurnRecord:
            turn = TurnRecord(
                turnId="turn-dest-1",
                threadId=thread_id,
                idx=1,
                userText=text,
                status="running",
                startedAt=utc_now(),
            )
            db.upsert_turn(turn)
            return turn

        manager.start_turn = fake_start_turn  # type: ignore[method-assign]

        result = asyncio.run(manager.commit_import_preview(preview.previewId, True, "copied context"))
        turn = db.get_turn("dest", "turn-dest-1")

        assert result["importedIntoTurnId"] == "turn-dest-1"
        assert result["turn"]["turnId"] == "turn-dest-1"
        assert turn is not None
        assert turn.metadata["contextLinks"] == [
            {
                "kind": "contextImport",
                "sourceThreadId": "source",
                "sourceTurnId": "turn-source-1",
                "previewId": preview.previewId,
                "linkedAt": turn.metadata["contextLinks"][0]["linkedAt"],
            },
            {
                "kind": "contextImport",
                "sourceThreadId": "source",
                "sourceTurnId": "turn-source-2",
                "previewId": preview.previewId,
                "linkedAt": turn.metadata["contextLinks"][1]["linkedAt"],
            },
        ]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_commit_import_preview_can_create_child_branch_from_destination_turn() -> None:
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
                turnId="turn-source-1",
                threadId="source",
                idx=1,
                userText="source prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId="turn-dest-2",
                threadId="dest",
                idx=2,
                userText="dest prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId="turn-dest-3",
                threadId="dest",
                idx=3,
                userText="dest head prompt",
                status="completed",
                startedAt=now,
            )
        )

        preview = asyncio.run(
            manager.create_import_preview("source", ["turn-source-1"], "dest", dest_turn_id="turn-dest-2")
        )

        async def fake_branch_from_turn(thread_id: str, turn_id: str, title: str | None = None) -> ThreadRecord:
            assert thread_id == "dest"
            assert turn_id == "turn-dest-2"
            child = ThreadRecord(
                threadId="dest-child",
                title="Dest child",
                createdAt=utc_now(),
                updatedAt=utc_now(),
                parentThreadId="dest",
                forkedFromTurnId="turn-dest-2",
            )
            db.upsert_thread(child)
            return child

        async def fake_start_turn(thread_id: str, text: str) -> TurnRecord:
            turn = TurnRecord(
                turnId="turn-child-3",
                threadId=thread_id,
                idx=3,
                userText=text,
                status="running",
                startedAt=utc_now(),
            )
            db.upsert_turn(turn)
            return turn

        manager.branch_from_turn = fake_branch_from_turn  # type: ignore[method-assign]
        manager.start_turn = fake_start_turn  # type: ignore[method-assign]

        result = asyncio.run(manager.commit_import_preview(preview.previewId, True, "copied context"))
        turn = db.get_turn("dest-child", "turn-child-3")

        assert result["thread"]["threadId"] == "dest-child"
        assert result["turn"]["threadId"] == "dest-child"
        assert turn is not None
        assert turn.metadata["contextLinks"] == [
            {
                "kind": "contextImport",
                "sourceThreadId": "source",
                "sourceTurnId": "turn-source-1",
                "previewId": preview.previewId,
                "linkedAt": turn.metadata["contextLinks"][0]["linkedAt"],
            }
        ]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_commit_import_preview_continues_existing_head_thread() -> None:
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
                turnId="turn-source-1",
                threadId="source",
                idx=1,
                userText="source prompt",
                status="completed",
                startedAt=now,
            )
        )
        db.upsert_turn(
            TurnRecord(
                turnId="turn-dest-6",
                threadId="dest",
                idx=6,
                userText="dest head prompt",
                status="completed",
                startedAt=now,
            )
        )
        preview = asyncio.run(
            manager.create_import_preview("source", ["turn-source-1"], "dest", dest_turn_id="turn-dest-6")
        )

        async def fake_start_turn(thread_id: str, text: str) -> TurnRecord:
            turn = TurnRecord(
                turnId="turn-dest-7",
                threadId=thread_id,
                idx=7,
                userText=text,
                status="running",
                startedAt=utc_now(),
            )
            db.upsert_turn(turn)
            return turn

        async def unexpected_branch_from_turn(thread_id: str, turn_id: str, title: str | None = None) -> ThreadRecord:
            raise AssertionError(f"branch_from_turn should not be called for head continuation: {thread_id} {turn_id} {title}")

        manager.start_turn = fake_start_turn  # type: ignore[method-assign]
        manager.branch_from_turn = unexpected_branch_from_turn  # type: ignore[method-assign]

        result = asyncio.run(manager.commit_import_preview(preview.previewId, True, "copied context"))

        assert result["thread"] is None
        assert result["turn"]["threadId"] == "dest"
        assert result["turn"]["turnId"] == "turn-dest-7"
        assert db.get_turn("dest", "turn-dest-7") is not None
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_sync_thread_snapshot_preserves_existing_turn_context_links() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-a", title="Thread A", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="turn-1",
                threadId="thread-a",
                idx=1,
                userText="prompt",
                status="completed",
                startedAt=now,
                metadata={"contextLinks": [{"sourceThreadId": "source", "sourceTurnId": "turn-source"}]},
            )
        )

        manager._sync_thread_snapshot(
            {
                "id": "thread-a",
                "createdAt": 0,
                "updatedAt": 0,
                "turns": [{"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "text": "answer"}]}],
            }
        )

        turn = db.get_turn("thread-a", "turn-1")
        assert turn is not None
        assert turn.metadata["contextLinks"] == [{"sourceThreadId": "source", "sourceTurnId": "turn-source"}]
        assert turn.metadata["items"] == [{"type": "agentMessage", "text": "answer"}]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_response_history_omits_null_local_shell_action_fields() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        history = manager._response_items_from_thread_items(
            [
                {
                    "type": "commandExecution",
                    "id": "cmd-1",
                    "status": "completed",
                    "command": "python -V",
                    "cwd": None,
                    "env": None,
                    "user": None,
                }
            ]
        )

        assert history == [
            {
                "type": "local_shell_call",
                "call_id": "cmd-1",
                "status": "completed",
                "action": {
                    "type": "exec",
                    "command": ["python", "-V"],
                },
            }
        ]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


class FakeRpc:
    def __init__(self) -> None:
        self.responses: list[tuple[object, object, object]] = []

    async def send_response(self, request_id, result=None, error=None) -> None:
        self.responses.append((request_id, result, error))

    async def close(self) -> None:
        return None


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


def test_ensure_capacity_retires_oldest_idle_session() -> None:
    temp_root = make_temp_root()
    settings = make_settings(temp_root)
    db = Database(settings.db_path)
    manager = CodexManager(db=db, ws=WebSocketHub(), settings=settings)
    try:
        oldest = CodexSession(process_key="oldest", rpc=FakeRpc(), thread_id="thread-oldest", last_used_monotonic=10.0)
        newer = CodexSession(process_key="newer", rpc=FakeRpc(), thread_id="thread-newer", last_used_monotonic=20.0)
        newest = CodexSession(process_key="newest", rpc=FakeRpc(), thread_id="thread-newest", last_used_monotonic=30.0)
        busy = CodexSession(
            process_key="busy",
            rpc=FakeRpc(),
            thread_id="thread-busy",
            last_used_monotonic=1.0,
            active_turn_id="turn-busy",
        )
        manager.sessions = {
            oldest.thread_id: oldest,
            newer.thread_id: newer,
            newest.thread_id: newest,
            busy.thread_id: busy,
        }
        retired: list[str] = []

        async def fake_retire_session(session: CodexSession) -> None:
            retired.append(session.process_key)

        manager._retire_session = fake_retire_session  # type: ignore[method-assign]

        asyncio.run(manager._ensure_capacity())

        assert retired == ["oldest"]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
