from __future__ import annotations

import asyncio
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .codex_rpc import CodexRpcClient, JsonRpcError
from .db import Database
from .models import ApprovalRecord, ImportPreviewRecord, ThreadRecord, TurnRecord
from .settings import Settings
from .util import APP_NAME, APP_VERSION, parse_codex_version, resolve_subprocess_command, split_command, utc_now
from .ws import WebSocketHub


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


@dataclass
class PendingTurn:
    idx: int
    user_text: str


@dataclass
class ApprovalHandle:
    request_id: Any
    method: str
    params: dict[str, Any]


@dataclass
class CodexSession:
    process_key: str
    rpc: CodexRpcClient
    thread_id: str | None = None
    event_seq_by_turn: dict[str, int] = field(default_factory=dict)
    active_turn_id: str | None = None
    pending_turn: PendingTurn | None = None
    pending_approvals: dict[str, ApprovalHandle] = field(default_factory=dict)
    last_used_monotonic: float = 0.0
    restart_attempted: bool = False


class CodexManager:
    def __init__(self, db: Database, ws: WebSocketHub, settings: Settings) -> None:
        self.db = db
        self.ws = ws
        self.settings = settings
        self.sessions: dict[str, CodexSession] = {}
        self._session_lock = asyncio.Lock()
        self._stopping = False

    async def verify_codex_installation(self) -> str:
        command = resolve_subprocess_command(split_command(self.settings.codex_bin) + ["--version"])
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        version = parse_codex_version(stdout.decode("utf-8", errors="replace"))
        if process.returncode != 0 or version is None:
            raise RuntimeError("Unable to determine Codex CLI version")
        if not re.match(self.settings.supported_codex_version_pattern, version):
            raise RuntimeError(f"Unsupported Codex CLI version {version}")
        return version

    async def ensure_schema(self) -> None:
        command = resolve_subprocess_command(split_command(self.settings.codex_bin) + [
            "app-server",
            "generate-json-schema",
            "--out",
            str(self.settings.schema_cache_dir),
        ])
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Unable to generate Codex app-server schema")
        self._verify_schema_files()

    def _verify_schema_files(self) -> None:
        required_client_methods = {
            "initialize",
            "thread/start",
            "thread/resume",
            "thread/fork",
            "thread/list",
            "thread/read",
            "turn/start",
        }
        required_notifications = {
            "thread/started",
            "thread/status/changed",
            "turn/started",
            "turn/completed",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
        }
        required_server_requests = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }
        client_schema = (self.settings.schema_cache_dir / "ClientRequest.json").read_text(encoding="utf-8")
        notification_schema = (self.settings.schema_cache_dir / "ServerNotification.json").read_text(encoding="utf-8")
        request_schema = (self.settings.schema_cache_dir / "ServerRequest.json").read_text(encoding="utf-8")
        for method in required_client_methods:
            if method not in client_schema:
                raise RuntimeError(f"Missing required client method in schema: {method}")
        for method in required_notifications:
            if method not in notification_schema:
                raise RuntimeError(f"Missing required notification in schema: {method}")
        for method in required_server_requests:
            if method not in request_schema:
                raise RuntimeError(f"Missing required server request in schema: {method}")

    async def start_thread(self, title: str | None = None) -> ThreadRecord:
        session = await self._spawn_session()
        result = await session.rpc.request_with_retry("thread/start", self._thread_start_params(), timeout_s=60)
        thread = result["thread"]
        session.thread_id = thread["id"]
        session.last_used_monotonic = asyncio.get_running_loop().time()
        async with self._session_lock:
            self.sessions[session.thread_id] = session
        thread_record = self._thread_record_from_codex(thread, title=title)
        self.db.upsert_thread(thread_record)
        await self.ws.emit_thread_created(thread_record)
        return thread_record

    async def list_threads(self) -> list[ThreadRecord]:
        return self.db.list_threads()

    async def get_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.db.get_thread(thread_id)
        if not thread:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        return thread

    async def get_or_resume_session(self, thread_id: str) -> CodexSession:
        async with self._session_lock:
            session = self.sessions.get(thread_id)
        if session:
            session.last_used_monotonic = asyncio.get_running_loop().time()
            return session
        session = await self._spawn_session()
        result = await session.rpc.request_with_retry("thread/resume", self._thread_resume_params(thread_id), timeout_s=60)
        thread = result["thread"]
        session.thread_id = thread["id"]
        session.last_used_monotonic = asyncio.get_running_loop().time()
        self._sync_thread_snapshot(thread, parent_thread_id=None, forked_from_turn_id=None)
        async with self._session_lock:
            self.sessions[thread_id] = session
        return session

    async def fork_thread(self, thread_id: str, title: str | None = None) -> ThreadRecord:
        parent_session = await self.get_or_resume_session(thread_id)
        parent_turn_id = self.db.get_last_turn_id(thread_id)
        result = await parent_session.rpc.request_with_retry(
            "thread/fork",
            {"threadId": thread_id, "persistExtendedHistory": True},
            timeout_s=60,
        )
        child_thread = result["thread"]
        child_thread_id = child_thread["id"]
        resumed = await self._spawn_session()
        resumed_result = await resumed.rpc.request_with_retry(
            "thread/resume",
            self._thread_resume_params(child_thread_id),
            timeout_s=60,
        )
        resumed.thread_id = child_thread_id
        resumed.last_used_monotonic = asyncio.get_running_loop().time()
        async with self._session_lock:
            self.sessions[child_thread_id] = resumed
        thread_record = self._sync_thread_snapshot(
            resumed_result["thread"],
            parent_thread_id=thread_id,
            forked_from_turn_id=parent_turn_id,
            title=title,
        )
        await self.ws.emit_thread_forked(thread_record)
        return thread_record

    async def start_turn(self, thread_id: str, text: str) -> TurnRecord:
        session = await self.get_or_resume_session(thread_id)
        if session.active_turn_id or session.pending_turn:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "turn_in_progress", "message": "Thread already has an active turn", "details": {}}},
            )
        pending = PendingTurn(idx=self.db.get_next_turn_index(thread_id), user_text=text)
        session.pending_turn = pending
        session.last_used_monotonic = asyncio.get_running_loop().time()
        try:
            result = await session.rpc.request_with_retry(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text, "text_elements": []}],
                },
                timeout_s=600,
            )
        except JsonRpcError as exc:
            session.pending_turn = None
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "code": "codex_rpc_error",
                        "message": exc.message,
                        "details": {"rpcCode": exc.code, "rpcData": exc.data},
                    }
                },
            ) from exc
        turn_data = result["turn"]
        turn = self._ensure_turn_record(thread_id, turn_data["id"], turn_data.get("status", "running"), pending)
        session.active_turn_id = turn.turnId
        session.pending_turn = None
        await self.ws.emit_turn_updated(turn)
        return turn

    async def respond_approval(self, approval_id: str, decision: str) -> ApprovalRecord:
        approval = self.db.get_approval(approval_id)
        if not approval or approval.status != "pending":
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "approval_not_found", "message": f"Unknown approval: {approval_id}", "details": {}}},
            )
        async with self._session_lock:
            session = self.sessions.get(approval.threadId)
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

    async def create_import_preview(self, source_thread_id: str, source_turn_ids: list[str], dest_thread_id: str) -> ImportPreviewRecord:
        await self.get_thread(source_thread_id)
        await self.get_thread(dest_thread_id)
        blob = self._build_transfer_blob(source_thread_id, source_turn_ids)
        preview = ImportPreviewRecord(
            previewId=f"imp_prev_{uuid.uuid4().hex}",
            destThreadId=dest_thread_id,
            sourceThreadId=source_thread_id,
            sourceTurnIds=source_turn_ids,
            suspectedSecrets=self._detect_suspected_secrets(blob),
            transferBlob=blob,
            expiresAt=self._plus_seconds(self.settings.import_preview_ttl_s),
        )
        self.db.save_import_preview(preview)
        return preview

    async def commit_import_preview(self, preview_id: str, confirmed: bool, edited_transfer_blob: str) -> dict[str, Any]:
        preview = self.db.get_import_preview(preview_id)
        if not preview:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "invalid_request", "message": "Unknown import preview", "details": {}}},
            )
        if not confirmed:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "import_preview_required", "message": "Import must be explicitly confirmed", "details": {}}},
            )
        turn = await self.start_turn(preview.destThreadId, edited_transfer_blob)
        self.db.delete_import_preview(preview_id)
        return {"importedIntoTurnId": turn.turnId, "destThreadId": turn.threadId, "status": turn.status}

    async def housekeeping_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(30)
            self.db.delete_expired_import_previews(utc_now())
            await self._evict_idle_sessions()

    async def close(self) -> None:
        self._stopping = True
        async with self._session_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await session.rpc.close()

    async def _evict_idle_sessions(self) -> None:
        cutoff = asyncio.get_running_loop().time() - self.settings.session_idle_ttl_s
        async with self._session_lock:
            idle_sessions = [
                session
                for session in self.sessions.values()
                if session.last_used_monotonic < cutoff and not session.active_turn_id and not session.pending_turn
            ]
        for session in idle_sessions:
            await self._retire_session(session)

    async def _retire_session(self, session: CodexSession) -> None:
        if session.thread_id:
            async with self._session_lock:
                current = self.sessions.get(session.thread_id)
                if current is session:
                    self.sessions.pop(session.thread_id, None)
        await session.rpc.close()

    async def _spawn_session(self) -> CodexSession:
        await self._ensure_capacity()
        command = resolve_subprocess_command(split_command(self.settings.codex_bin) + ["app-server"])
        process_key = uuid.uuid4().hex
        session_ref: dict[str, CodexSession] = {}

        async def notification_handler(msg: dict[str, Any]) -> None:
            await self._handle_notification(session_ref["session"], msg)

        async def server_request_handler(msg: dict[str, Any]) -> None:
            await self._handle_server_request(session_ref["session"], msg)

        async def stderr_handler(line: str) -> None:
            await self._handle_stderr(session_ref["session"], line)

        async def exit_handler(code: int | None) -> None:
            await self._handle_session_exit(session_ref["session"], code)

        rpc = await CodexRpcClient.start(
            command=command,
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            stderr_handler=stderr_handler,
            exit_handler=exit_handler,
        )
        session = CodexSession(process_key=process_key, rpc=rpc, last_used_monotonic=asyncio.get_running_loop().time())
        session_ref["session"] = session
        await rpc.request(
            "initialize",
            {
                "clientInfo": {"name": APP_NAME, "title": "Codex UI Wrapper", "version": APP_VERSION},
                "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
            },
            timeout_s=30,
        )
        await rpc.notify("initialized")
        return session

    async def _ensure_capacity(self) -> None:
        async with self._session_lock:
            sessions = list(self.sessions.values())
        if len(sessions) < self.settings.session_limit:
            return
        idle = sorted(
            [session for session in sessions if not session.active_turn_id and not session.pending_turn],
            key=lambda item: item.last_used_monotonic,
        )
        if not idle:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "codex_process_unavailable", "message": "All Codex sessions are busy", "details": {}}},
            )
        await self._retire_session(idle[0])

    async def _handle_notification(self, session: CodexSession, msg: dict[str, Any]) -> None:
        method = msg["method"]
        params = msg.get("params", {})
        thread_id = self._extract_thread_id(params) or session.thread_id
        if not thread_id:
            return
        session.thread_id = thread_id
        turn_id = self._extract_turn_id(params)
        seq_key = turn_id or "__thread__"
        seq = session.event_seq_by_turn.get(seq_key, 0) + 1
        session.event_seq_by_turn[seq_key] = seq
        event = self.db.append_event(thread_id, turn_id, seq, method, params)
        await self.ws.emit_event(event)
        session.last_used_monotonic = asyncio.get_running_loop().time()
        await self._apply_notification_side_effects(session, method, params)

    async def _apply_notification_side_effects(self, session: CodexSession, method: str, params: dict[str, Any]) -> None:
        thread_id = self._extract_thread_id(params) or session.thread_id
        if not thread_id:
            return
        if method == "thread/started":
            thread_record = self._sync_thread_snapshot(params["thread"], title=None)
            await self.ws.emit_thread_updated(thread_record)
            return
        if method == "thread/status/changed":
            thread = self.db.update_thread_status(thread_id, self._normalize_thread_status(params.get("status")))
            if thread:
                await self.ws.emit_thread_updated(thread)
            return
        if method == "turn/started":
            turn = params["turn"]
            pending = session.pending_turn or PendingTurn(self.db.get_next_turn_index(thread_id), "")
            turn_record = self._ensure_turn_record(thread_id, turn["id"], turn.get("status", "running"), pending)
            session.active_turn_id = turn_record.turnId
            await self.ws.emit_turn_updated(turn_record)
            return
        if method == "turn/completed":
            turn = params["turn"]
            turn_record = self.db.update_turn_status(
                thread_id,
                turn["id"],
                self._normalize_turn_status(turn.get("status"), fallback="completed"),
                completed_at=utc_now(),
            )
            session.active_turn_id = None
            session.pending_turn = None
            if turn_record:
                await self.ws.emit_turn_updated(turn_record)
            return
        if method == "error":
            turn_id = params.get("turnId")
            if turn_id:
                turn_record = self.db.update_turn_status(
                    thread_id,
                    turn_id,
                    "error",
                    completed_at=utc_now(),
                    metadata={"error": params.get("error")},
                )
                session.active_turn_id = None
                session.pending_turn = None
                if turn_record:
                    await self.ws.emit_turn_updated(turn_record)

    async def _handle_server_request(self, session: CodexSession, msg: dict[str, Any]) -> None:
        method = msg["method"]
        if method not in APPROVAL_METHODS:
            await session.rpc.send_response(str(msg["id"]), error={"code": -32601, "message": f"Unsupported server request: {method}"})
            return
        params = msg.get("params", {})
        approval_id = params.get("approvalId") or f"approval-{uuid.uuid4().hex}"
        approval = ApprovalRecord(
            approvalId=approval_id,
            threadId=params.get("threadId") or session.thread_id or "",
            turnId=params.get("turnId"),
            itemId=params.get("itemId"),
            requestId=str(msg["id"]),
            requestMethod=method,
            status="pending",
            details=params,
            createdAt=utc_now(),
            updatedAt=utc_now(),
        )
        session.pending_approvals[approval_id] = ApprovalHandle(request_id=msg["id"], method=method, params=params)
        self.db.upsert_approval(approval)
        await self.ws.emit_approval_requested(approval)

    async def _handle_stderr(self, session: CodexSession, line: str) -> None:
        if not session.thread_id:
            return
        seq = session.event_seq_by_turn.get("__thread__", 0) + 1
        session.event_seq_by_turn["__thread__"] = seq
        event = self.db.append_event(session.thread_id, None, seq, "codex/stderr", {"line": line})
        await self.ws.emit_event(event)

    async def _handle_session_exit(self, session: CodexSession, code: int | None) -> None:
        if self._stopping or not session.thread_id:
            return
        async with self._session_lock:
            current = self.sessions.get(session.thread_id)
            if current is session:
                self.sessions.pop(session.thread_id, None)
        if not session.restart_attempted:
            session.restart_attempted = True
            try:
                resumed = await self._spawn_session()
                result = await resumed.rpc.request_with_retry(
                    "thread/resume",
                    self._thread_resume_params(session.thread_id),
                    timeout_s=60,
                )
                resumed.thread_id = session.thread_id
                resumed.restart_attempted = True
                resumed.last_used_monotonic = asyncio.get_running_loop().time()
                self._sync_thread_snapshot(result["thread"], title=None)
                async with self._session_lock:
                    self.sessions[session.thread_id] = resumed
                thread = self.db.update_thread_status(session.thread_id, "idle", metadata={"lastRestartExitCode": code})
                if thread:
                    await self.ws.emit_thread_updated(thread)
                return
            except Exception as exc:
                thread = self.db.update_thread_status(session.thread_id, "dead", metadata={"restartError": str(exc), "lastExitCode": code})
                if thread:
                    await self.ws.emit_thread_updated(thread)
                return
        thread = self.db.update_thread_status(session.thread_id, "dead", metadata={"lastExitCode": code})
        if thread:
            await self.ws.emit_thread_updated(thread)

    def _sync_thread_snapshot(
        self,
        codex_thread: dict[str, Any],
        parent_thread_id: str | None = None,
        forked_from_turn_id: str | None = None,
        title: str | None = None,
    ) -> ThreadRecord:
        thread_record = self._thread_record_from_codex(
            codex_thread,
            title=title,
            parent_thread_id=parent_thread_id,
            forked_from_turn_id=forked_from_turn_id,
        )
        self.db.upsert_thread(thread_record)
        for index, turn in enumerate(codex_thread.get("turns", []), start=1):
            existing = self.db.get_turn(thread_record.threadId, turn["id"])
            items = turn.get("items", [])
            self.db.upsert_turn(
                TurnRecord(
                    turnId=turn["id"],
                    threadId=thread_record.threadId,
                    idx=existing.idx if existing else index,
                    userText=existing.userText if existing and existing.userText else self._extract_user_text_from_items(items),
                    status=self._normalize_turn_status(turn.get("status"), fallback=existing.status if existing else "completed"),
                    startedAt=existing.startedAt if existing else thread_record.createdAt,
                    completedAt=existing.completedAt if existing else None,
                    metadata={"items": items},
                )
            )
        return thread_record

    def _thread_record_from_codex(
        self,
        codex_thread: dict[str, Any],
        title: str | None = None,
        parent_thread_id: str | None = None,
        forked_from_turn_id: str | None = None,
    ) -> ThreadRecord:
        existing = self.db.get_thread(codex_thread["id"])
        return ThreadRecord(
            threadId=codex_thread["id"],
            title=title or codex_thread.get("name") or codex_thread.get("preview") or (existing.title if existing else "Untitled thread"),
            createdAt=self._from_unix(codex_thread.get("createdAt")),
            updatedAt=self._from_unix(codex_thread.get("updatedAt")),
            parentThreadId=parent_thread_id if parent_thread_id is not None else (existing.parentThreadId if existing else None),
            forkedFromTurnId=forked_from_turn_id if forked_from_turn_id is not None else (existing.forkedFromTurnId if existing else None),
            status=self._normalize_thread_status(codex_thread.get("status"), fallback=existing.status if existing else "idle"),
            metadata={
                "preview": codex_thread.get("preview"),
                "cwd": codex_thread.get("cwd"),
                "path": codex_thread.get("path"),
                "cliVersion": codex_thread.get("cliVersion"),
                "modelProvider": codex_thread.get("modelProvider"),
                "source": codex_thread.get("source"),
            },
        )

    def _ensure_turn_record(self, thread_id: str, turn_id: str, status: str, pending: PendingTurn) -> TurnRecord:
        existing = self.db.get_turn(thread_id, turn_id)
        turn = TurnRecord(
            turnId=turn_id,
            threadId=thread_id,
            idx=existing.idx if existing else pending.idx,
            userText=existing.userText if existing else pending.user_text,
            status=self._normalize_turn_status(status, fallback=existing.status if existing else "running"),
            startedAt=existing.startedAt if existing else utc_now(),
            completedAt=existing.completedAt if existing else None,
            metadata=existing.metadata if existing else {},
        )
        self.db.upsert_turn(turn)
        return turn

    def _normalize_thread_status(self, status: Any, fallback: str = "idle") -> str:
        if isinstance(status, str):
            return status
        if isinstance(status, dict):
            status_type = status.get("type")
            if status_type == "active":
                return "running"
            if status_type == "systemError":
                return "error"
            if isinstance(status_type, str):
                return status_type
        return fallback

    def _normalize_turn_status(self, status: Any, fallback: str = "running") -> str:
        if not isinstance(status, str):
            return fallback
        if status == "inProgress":
            return "running"
        if status == "failed":
            return "error"
        if status == "interrupted":
            return "interrupted"
        return status

    def _thread_start_params(self) -> dict[str, Any]:
        return {
            "cwd": str(self.settings.workspace_dir),
            "approvalPolicy": self.settings.approval_policy,
            "ephemeral": False,
            "experimentalRawEvents": False,
            "persistExtendedHistory": True,
            "serviceName": APP_NAME,
        }

    def _thread_resume_params(self, thread_id: str) -> dict[str, Any]:
        return {
            "threadId": thread_id,
            "cwd": str(self.settings.workspace_dir),
            "approvalPolicy": self.settings.approval_policy,
            "persistExtendedHistory": True,
        }

    def _approval_result(self, method: str, decision: str) -> dict[str, Any]:
        approve = decision == "approve"
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept" if approve else "decline"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept" if approve else "decline"}
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "approved" if approve else "denied"}
        return {"decision": "decline"}

    def _build_transfer_blob(self, source_thread_id: str, source_turn_ids: list[str]) -> str:
        lines = [f"Imported context from thread {source_thread_id}", ""]
        for turn_id in source_turn_ids:
            turn = self.db.get_turn(source_thread_id, turn_id)
            if not turn or turn.threadId != source_thread_id:
                continue
            lines.append(f"Turn {turn.turnId}")
            lines.append(f"User: {turn.userText}")
            final_message = self._extract_final_agent_text(source_thread_id, turn_id)
            if final_message:
                lines.append(f"Assistant: {final_message}")
            command_summaries = self._extract_command_summaries(source_thread_id, turn_id)
            if command_summaries:
                lines.append("Commands:")
                lines.extend(f"- {summary}" for summary in command_summaries)
            lines.append("")
        lines.append("This is copied context, not a true merge.")
        return "\n".join(lines).strip()

    def _extract_final_agent_text(self, thread_id: str, turn_id: str) -> str:
        chunks: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type == "item/agentMessage/delta":
                chunks.append(str(event.payload.get("delta", "")))
                continue
            if event.type == "item/completed":
                item = event.payload.get("item", {})
                if item.get("type") == "agentMessage" and item.get("text"):
                    return str(item["text"])
        return "".join(chunks).strip()

    def _extract_command_summaries(self, thread_id: str, turn_id: str) -> list[str]:
        summaries: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type != "item/completed":
                continue
            item = event.payload.get("item", {})
            if item.get("type") != "commandExecution":
                continue
            summaries.append(f"{item.get('command', '')} [{item.get('status', 'unknown')}] exit={item.get('exitCode')}")
        return summaries

    def _detect_suspected_secrets(self, text: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        patterns = [
            ("Possible OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
            ("Possible GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
            ("Possible AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ]
        for label, pattern in patterns:
            for match in pattern.finditer(text):
                findings.append({"label": label, "start": match.start(), "end": match.end()})
        for match in re.finditer(r"[A-Za-z0-9_\\-]{24,}", text):
            token = match.group(0)
            if self._looks_high_entropy(token):
                findings.append({"label": "High-entropy token-like string", "start": match.start(), "end": match.end()})
        return findings

    def _looks_high_entropy(self, token: str) -> bool:
        if len(token) < 24:
            return False
        normalized = token.replace("-", "")
        if len(normalized) >= 24 and re.fullmatch(r"[0-9a-fA-F]+", normalized):
            return False
        alphabet = set(token)
        if len(alphabet) < 8:
            return False
        probabilities = [token.count(char) / len(token) for char in alphabet]
        entropy = -sum(prob * math.log2(prob) for prob in probabilities)
        return entropy > 3.5

    def _extract_user_text_from_items(self, items: list[dict[str, Any]]) -> str:
        for item in items:
            if item.get("type") != "userMessage":
                continue
            text_parts = [
                part.get("text", "")
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "".join(text_parts).strip()
            if text:
                return text
        return ""

    def _extract_thread_id(self, payload: dict[str, Any]) -> str | None:
        if "threadId" in payload:
            return payload["threadId"]
        thread = payload.get("thread")
        if isinstance(thread, dict):
            return thread.get("id")
        return None

    def _extract_turn_id(self, payload: dict[str, Any]) -> str | None:
        if "turnId" in payload:
            return payload["turnId"]
        turn = payload.get("turn")
        if isinstance(turn, dict):
            return turn.get("id")
        return None

    def _from_unix(self, value: int | float | None) -> str:
        if value is None:
            return utc_now()
        return datetime.fromtimestamp(float(value), tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _plus_seconds(self, seconds: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
