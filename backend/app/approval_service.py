from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, MutableMapping, Set
from typing import Any

from fastapi import HTTPException

from .db import Database
from .models import ApprovalRecord
from .util import utc_now
from .ws import WebSocketHub


MakeApprovalHandleFn = Callable[[Any, str, dict[str, Any]], Any]
ApprovalResultFn = Callable[[str, str], dict[str, Any]]


def approval_result_for_method(method: str, decision: str) -> dict[str, Any]:
    approve = decision == "approve"
    if method == "item/commandExecution/requestApproval":
        return {"decision": "accept" if approve else "decline"}
    if method == "item/fileChange/requestApproval":
        return {"decision": "accept" if approve else "decline"}
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {"decision": "approved" if approve else "denied"}
    return {"decision": "decline"}


class ApprovalService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        sessions: MutableMapping[str, Any],
        session_lock: asyncio.Lock,
        approval_methods: Set[str],
        make_approval_handle: MakeApprovalHandleFn,
        approval_result: ApprovalResultFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._sessions = sessions
        self._session_lock = session_lock
        self._approval_methods = set(approval_methods)
        self._make_approval_handle = make_approval_handle
        self._approval_result = approval_result

    async def handle_server_request(self, session: Any, msg: dict[str, Any]) -> None:
        method = msg["method"]
        if method not in self._approval_methods:
            await session.rpc.send_response(str(msg["id"]), error={"code": -32601, "message": f"Unsupported server request: {method}"})
            return
        params = msg.get("params", {})
        approval_id = params.get("approvalId") or f"approval-{uuid.uuid4().hex}"
        approval = ApprovalRecord(
            approvalId=approval_id,
            threadId=session.local_thread_id or params.get("threadId") or session.thread_id or "",
            turnId=params.get("turnId"),
            itemId=params.get("itemId"),
            requestId=str(msg["id"]),
            requestMethod=method,
            status="pending",
            details=params,
            createdAt=utc_now(),
            updatedAt=utc_now(),
        )
        session.pending_approvals[approval_id] = self._make_approval_handle(msg["id"], method, params)
        self.db.upsert_approval(approval)
        await self.ws.emit_approval_requested(approval)

    async def respond_approval(self, approval_id: str, decision: str) -> ApprovalRecord:
        approval = self.db.get_approval(approval_id)
        if not approval or approval.status != "pending":
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "approval_not_found", "message": f"Unknown approval: {approval_id}", "details": {}}},
            )
        async with self._session_lock:
            session = self._sessions.get(approval.threadId)
        if not session:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "codex_process_unavailable", "message": "No active Codex session for this approval", "details": {}}},
            )
        handle = session.pending_approvals.get(approval_id)
        if not handle:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "approval_not_found", "message": f"Approval is no longer pending: {approval_id}", "details": {}}},
            )
        await session.rpc.send_response(handle.request_id, result=self._approval_result(handle.method, decision))
        session.pending_approvals.pop(approval_id, None)
        approval = self.db.update_approval_status(approval_id, "approve" if decision == "approve" else "deny")
        assert approval is not None
        await self.ws.emit_approval_responded(approval)
        return approval
