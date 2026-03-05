from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from backend.app.approval_service import ApprovalService, approval_result_for_method
from backend.app.db import Database
from backend.app.models import ApprovalRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeRpc:
    def __init__(self) -> None:
        self.responses: list[tuple[object, object, object]] = []

    async def send_response(self, request_id, result=None, error=None) -> None:
        self.responses.append((request_id, result, error))


class FakeWs:
    def __init__(self) -> None:
        self.requested: list[ApprovalRecord] = []
        self.responded: list[ApprovalRecord] = []

    async def emit_approval_requested(self, approval: ApprovalRecord) -> None:
        self.requested.append(approval)

    async def emit_approval_responded(self, approval: ApprovalRecord) -> None:
        self.responded.append(approval)


class SessionStub:
    def __init__(self, thread_id: str) -> None:
        self.local_thread_id: str | None = None
        self.thread_id = thread_id
        self.pending_approvals: dict[str, object] = {}
        self.rpc = FakeRpc()


def make_service(db: Database, ws: FakeWs, sessions: dict[str, SessionStub], session_lock: asyncio.Lock) -> ApprovalService:
    return ApprovalService(
        db=db,
        ws=ws,  # type: ignore[arg-type]
        sessions=sessions,
        session_lock=session_lock,
        approval_methods={
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        },
        make_approval_handle=lambda request_id, method, params: SimpleNamespace(request_id=request_id, method=method, params=params),
        approval_result=approval_result_for_method,
    )


def test_approval_result_for_method_matches_expected_mapping() -> None:
    assert approval_result_for_method("item/commandExecution/requestApproval", "approve") == {"decision": "accept"}
    assert approval_result_for_method("item/fileChange/requestApproval", "deny") == {"decision": "decline"}
    assert approval_result_for_method("execCommandApproval", "approve") == {"decision": "approved"}
    assert approval_result_for_method("applyPatchApproval", "deny") == {"decision": "denied"}
    assert approval_result_for_method("unknown", "approve") == {"decision": "decline"}


def test_handle_server_request_rejects_unsupported_method() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "approvals.db")
    ws = FakeWs()
    sessions: dict[str, SessionStub] = {}
    session_lock = asyncio.Lock()
    try:
        service = make_service(db, ws, sessions, session_lock)
        session = SessionStub("thread-1")

        asyncio.run(service.handle_server_request(session, {"id": 5, "method": "unknown/request", "params": {}}))

        assert session.rpc.responses == [
            ("5", None, {"code": -32601, "message": "Unsupported server request: unknown/request"})
        ]
        assert ws.requested == []
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_server_request_persists_pending_approval_and_emit() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "approvals.db")
    ws = FakeWs()
    sessions: dict[str, SessionStub] = {}
    session_lock = asyncio.Lock()
    try:
        service = make_service(db, ws, sessions, session_lock)
        session = SessionStub("thread-1")

        asyncio.run(
            service.handle_server_request(
                session,
                {
                    "id": 0,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "approvalId": "approval-1",
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                    },
                },
            )
        )

        approval = db.get_approval("approval-1")
        assert approval is not None
        assert approval.status == "pending"
        assert approval.requestMethod == "item/fileChange/requestApproval"
        assert "approval-1" in session.pending_approvals
        assert len(ws.requested) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_respond_approval_updates_db_and_responds_on_rpc() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "approvals.db")
    ws = FakeWs()
    sessions: dict[str, SessionStub] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_approval(
            ApprovalRecord(
                approvalId="approval-1",
                threadId="thread-1",
                turnId="turn-1",
                itemId="item-1",
                requestId="0",
                requestMethod="item/fileChange/requestApproval",
                status="pending",
                details={},
                createdAt=now,
                updatedAt=now,
            )
        )
        session = SessionStub("thread-1")
        session.pending_approvals["approval-1"] = SimpleNamespace(
            request_id=0,
            method="item/fileChange/requestApproval",
            params={},
        )
        sessions["thread-1"] = session
        service = make_service(db, ws, sessions, session_lock)

        result = asyncio.run(service.respond_approval("approval-1", "approve"))

        assert result.status == "approve"
        assert session.rpc.responses == [(0, {"decision": "accept"}, None)]
        assert "approval-1" not in session.pending_approvals
        assert len(ws.responded) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_respond_approval_requires_active_session() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "approvals.db")
    ws = FakeWs()
    sessions: dict[str, SessionStub] = {}
    session_lock = asyncio.Lock()
    try:
        now = utc_now()
        db.upsert_approval(
            ApprovalRecord(
                approvalId="approval-1",
                threadId="thread-1",
                turnId="turn-1",
                itemId="item-1",
                requestId="0",
                requestMethod="item/fileChange/requestApproval",
                status="pending",
                details={},
                createdAt=now,
                updatedAt=now,
            )
        )
        service = make_service(db, ws, sessions, session_lock)
        try:
            asyncio.run(service.respond_approval("approval-1", "deny"))
            raise AssertionError("Expected missing session to fail")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["error"]["code"] == "codex_process_unavailable"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
