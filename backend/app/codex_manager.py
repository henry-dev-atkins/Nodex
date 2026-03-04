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
    intentional_close: bool = False


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
        await self.ws.emit_thread_forked(thread_record, turns=self.db.list_turns(child_thread_id))
        return thread_record

    async def branch_from_turn(self, thread_id: str, turn_id: str, title: str | None = None) -> ThreadRecord:
        turn = self.db.get_turn(thread_id, turn_id)
        if not turn:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "turn_not_found", "message": f"Unknown turn: {turn_id}", "details": {}}},
            )
        if turn.status == "running":
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "turn_in_progress", "message": "Cannot branch from an active turn", "details": {}}},
            )
        parent_session = await self.get_or_resume_session(thread_id)
        source = await parent_session.rpc.request_with_retry(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
            timeout_s=60,
        )
        history = self._build_response_history(source["thread"], turn_id)
        child_session = await self._spawn_session()
        resumed = await child_session.rpc.request_with_retry(
            "thread/resume",
            self._thread_resume_params(thread_id, history=history),
            timeout_s=60,
        )
        child_thread = resumed["thread"]
        child_thread_id = child_thread["id"]
        child_session.thread_id = child_thread_id
        child_session.last_used_monotonic = asyncio.get_running_loop().time()
        async with self._session_lock:
            self.sessions[child_thread_id] = child_session
        thread_record = self._sync_thread_snapshot(
            child_thread,
            parent_thread_id=thread_id,
            forked_from_turn_id=turn_id,
            title=title,
        )
        await self.ws.emit_thread_forked(thread_record, turns=self.db.list_turns(child_thread_id))
        return thread_record

    async def delete_conversation(self, thread_id: str) -> dict[str, Any]:
        await self.get_thread(thread_id)
        conversation_id = self._conversation_root_id(thread_id)
        thread_ids = self.db.list_branch_thread_ids(conversation_id)
        if not thread_ids:
            return {"conversationId": conversation_id, "deletedThreadIds": []}
        async with self._session_lock:
            sessions = [self.sessions.get(item) for item in thread_ids]
        for session in sessions:
            if session is not None:
                await self._retire_session(session)
        self.db.delete_threads(thread_ids)
        for deleted_thread_id in thread_ids:
            await self.ws.emit_thread_deleted(deleted_thread_id, conversation_id)
        return {"conversationId": conversation_id, "deletedThreadIds": thread_ids}

    async def rename_thread(self, thread_id: str, title: str) -> ThreadRecord:
        clean_title = title.strip()
        if not clean_title:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_request", "message": "Title cannot be empty", "details": {}}},
            )
        await self.get_thread(thread_id)
        updated = self.db.update_thread_title(thread_id, clean_title)
        if not updated:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        await self.ws.emit_thread_updated(updated)
        return updated

    async def delete_branch(self, thread_id: str) -> dict[str, Any]:
        thread = await self.get_thread(thread_id)
        if not thread.parentThreadId:
            return await self.delete_conversation(thread_id)
        conversation_id = self._conversation_root_id(thread_id)
        thread_ids = self.db.list_branch_thread_ids(thread_id)
        if not thread_ids:
            return {"conversationId": conversation_id, "deletedThreadIds": []}
        async with self._session_lock:
            sessions = [self.sessions.get(item) for item in thread_ids]
        for session in sessions:
            if session is not None:
                await self._retire_session(session)
        self.db.delete_threads(thread_ids)
        for deleted_thread_id in thread_ids:
            await self.ws.emit_thread_deleted(deleted_thread_id, conversation_id)
        return {"conversationId": conversation_id, "deletedThreadIds": thread_ids}

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

    async def create_import_preview(
        self,
        source_thread_id: str,
        source_turn_ids: list[str],
        dest_thread_id: str,
        dest_turn_id: str | None = None,
    ) -> ImportPreviewRecord:
        await self.get_thread(source_thread_id)
        await self.get_thread(dest_thread_id)
        if dest_turn_id:
            dest_turn = self.db.get_turn(dest_thread_id, dest_turn_id)
            if not dest_turn:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": {
                            "code": "turn_not_found",
                            "message": f"Unknown destination turn: {dest_turn_id}",
                            "details": {},
                        }
                    },
                )
        blob = self._build_transfer_blob(source_thread_id, source_turn_ids)
        preview = ImportPreviewRecord(
            previewId=f"imp_prev_{uuid.uuid4().hex}",
            destThreadId=dest_thread_id,
            destTurnId=dest_turn_id,
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
        created_thread: ThreadRecord | None = None
        destination_thread_id = preview.destThreadId
        if preview.destTurnId:
            head_turn_id = self.db.get_last_turn_id(preview.destThreadId)
            if head_turn_id != preview.destTurnId:
                created_thread = await self.branch_from_turn(preview.destThreadId, preview.destTurnId, title=None)
                destination_thread_id = created_thread.threadId
        turn = await self.start_turn(destination_thread_id, edited_transfer_blob)
        turn = self._annotate_imported_turn(turn, preview)
        await self.ws.emit_turn_updated(turn)
        self.db.delete_import_preview(preview_id)
        branch_turns = self.db.list_turns(created_thread.threadId) if created_thread else None
        return {
            "importedIntoTurnId": turn.turnId,
            "destThreadId": turn.threadId,
            "status": turn.status,
            "turn": turn.model_dump(),
            "thread": created_thread.model_dump() if created_thread else None,
            "turns": [item.model_dump() for item in branch_turns] if branch_turns else None,
        }

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
        session.intentional_close = True
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
        if session.intentional_close:
            return
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
                    metadata={**(existing.metadata if existing else {}), "items": items},
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

    def _thread_resume_params(self, thread_id: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload = {
            "threadId": thread_id,
            "cwd": str(self.settings.workspace_dir),
            "approvalPolicy": self.settings.approval_policy,
            "persistExtendedHistory": True,
        }
        if history is not None:
            payload["history"] = history
        return payload

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
        source_thread = self.db.get_thread(source_thread_id)
        source_label = source_thread.title if source_thread and source_thread.title else source_thread_id
        valid_turns = [
            turn
            for turn_id in source_turn_ids
            for turn in [self.db.get_turn(source_thread_id, turn_id)]
            if turn and turn.threadId == source_thread_id
        ]
        lines = [
            "Copied branch context",
            "",
            f"Source branch: {source_label}",
            f"Source thread ID: {source_thread_id}",
            f"Selected turns: {len(valid_turns)}",
            "",
        ]
        for turn in valid_turns:
            final_message = self._extract_final_agent_text(source_thread_id, turn.turnId)
            reasoning_summary = self._extract_reasoning_summary(source_thread_id, turn.turnId)
            decision_summary = self._extract_decision_summary(source_thread_id, turn.turnId, turn.status)
            if final_message:
                result_text = final_message
            else:
                result_text = "No final assistant result captured yet."
            summary_text = reasoning_summary or final_message or turn.userText
            command_summaries = self._extract_command_summaries(source_thread_id, turn.turnId)
            lines.append(f"Turn {turn.idx} ({turn.turnId})")
            lines.append("Prompt:")
            lines.append(turn.userText)
            lines.append("")
            lines.append("Summary:")
            lines.append(summary_text)
            lines.append("")
            lines.append("Decision:")
            lines.append(decision_summary)
            lines.append("")
            lines.append("Result:")
            lines.append(result_text)
            if command_summaries:
                lines.append("")
                lines.append("Commands:")
                lines.extend(f"- {summary}" for summary in command_summaries)
            lines.append("")
        lines.append("This is copied context, not a true merge. Use it as reference material in the destination branch.")
        return "\n".join(lines).strip()

    def _annotate_imported_turn(self, turn: TurnRecord, preview: ImportPreviewRecord) -> TurnRecord:
        existing_links = turn.metadata.get("contextLinks", [])
        if not isinstance(existing_links, list):
            existing_links = []
        next_links = list(existing_links)
        linked_at = utc_now()
        for source_turn_id in preview.sourceTurnIds:
            next_links.append(
                {
                    "kind": "contextImport",
                    "sourceThreadId": preview.sourceThreadId,
                    "sourceTurnId": source_turn_id,
                    "previewId": preview.previewId,
                    "linkedAt": linked_at,
                }
            )
        updated = self.db.update_turn_status(
            turn.threadId,
            turn.turnId,
            turn.status,
            completed_at=turn.completedAt,
            metadata={"contextLinks": next_links},
        )
        return updated or turn

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

    def _extract_reasoning_summary(self, thread_id: str, turn_id: str) -> str:
        turn = self.db.get_turn(thread_id, turn_id)
        items = turn.metadata.get("items", []) if turn else []
        for item in items:
            if item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(str(part).strip() for part in summary if str(part).strip()).strip()
                if text:
                    return text
            text = str(item.get("text", "")).strip()
            if text:
                return text
        chunks: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type == "item/reasoning/summaryTextDelta":
                chunks.append(str(event.payload.get("delta", "")))
                continue
            if event.type != "item/completed":
                continue
            item = event.payload.get("item", {})
            if item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(str(part).strip() for part in summary if str(part).strip()).strip()
                if text:
                    return text
            text = str(item.get("text", "")).strip()
            if text:
                return text
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

    def _extract_decision_summary(self, thread_id: str, turn_id: str, turn_status: str) -> str:
        approvals = self.db.list_approvals(thread_id=thread_id, turn_id=turn_id)
        decisions = [approval for approval in approvals if approval.status in {"approve", "deny"}]
        if decisions:
            latest = decisions[-1]
            if latest.status == "approve":
                return "Approval granted for the requested action."
            return "Approval denied for the requested action."
        if turn_status == "error":
            return "The turn failed before it produced a stable result."
        if turn_status == "running":
            return "The turn is still running."
        if turn_status == "interrupted":
            return "The turn was interrupted before completion."
        if turn_status == "completed":
            return "Completed without an explicit approval decision."
        return f"Turn status: {turn_status}"

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

    def _conversation_root_id(self, thread_id: str) -> str:
        current = self.db.get_thread(thread_id)
        if not current:
            return thread_id
        while current.parentThreadId:
            parent = self.db.get_thread(current.parentThreadId)
            if not parent:
                break
            current = parent
        return current.threadId

    def _build_response_history(self, codex_thread: dict[str, Any], turn_id: str) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        found = False
        for turn in codex_thread.get("turns", []):
            history.extend(self._response_items_from_thread_items(turn.get("items", [])))
            if turn.get("id") == turn_id:
                found = True
                break
        if not found:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "turn_not_found", "message": f"Unknown turn: {turn_id}", "details": {}}},
            )
        return history

    def _sanitize_local_shell_action(self, item: dict[str, Any]) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "exec",
            "command": split_command(str(item.get("command", ""))),
        }
        working_directory = item.get("cwd") or item.get("working_directory")
        if working_directory is not None:
            action["working_directory"] = str(working_directory)
        timeout_ms = item.get("timeout_ms")
        if isinstance(timeout_ms, int) and timeout_ms >= 0:
            action["timeout_ms"] = timeout_ms
        user = item.get("user")
        if user is not None:
            action["user"] = str(user)
        env = item.get("env")
        if isinstance(env, dict):
            sanitized_env = {
                str(key): str(value)
                for key, value in env.items()
                if key is not None and value is not None
            }
            if sanitized_env:
                action["env"] = sanitized_env
        return action

    def _sanitize_message_history_item(
        self,
        role: str,
        content: list[dict[str, Any]],
        phase: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "message",
            "role": role,
            "content": content,
        }
        if phase is not None:
            item["phase"] = phase
        return item

    def _sanitize_reasoning_history_item(self, item: dict[str, Any]) -> dict[str, Any]:
        history_item: dict[str, Any] = {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text} for text in item.get("summary", [])],
        }
        content = [{"type": "reasoning_text", "text": text} for text in item.get("content", [])]
        if content:
            history_item["content"] = content
        encrypted_content = item.get("encrypted_content")
        if encrypted_content is not None:
            history_item["encrypted_content"] = encrypted_content
        return history_item

    def _sanitize_web_search_action(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_action = item.get("action")
        if isinstance(raw_action, dict):
            return {
                str(key): value
                for key, value in raw_action.items()
                if key is not None and value is not None
            }
        action = {"type": "search"}
        query = item.get("query")
        if query is not None:
            action["query"] = str(query)
        return action

    def _response_items_from_thread_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for item in items:
            item_type = item.get("type")
            if item_type == "userMessage":
                history.append(self._sanitize_message_history_item(
                    "user",
                    [self._content_item_from_user_input(part) for part in item.get("content", [])],
                ))
                continue
            if item_type == "agentMessage":
                history.append(self._sanitize_message_history_item(
                    "assistant",
                    [{"type": "output_text", "text": item.get("text", "")}],
                    phase=item.get("phase"),
                ))
                continue
            if item_type == "plan":
                history.append(self._sanitize_message_history_item(
                    "assistant",
                    [{"type": "output_text", "text": item.get("text", "")}],
                    phase="commentary",
                ))
                continue
            if item_type == "reasoning":
                history.append(self._sanitize_reasoning_history_item(item))
                continue
            if item_type == "commandExecution":
                history.append(
                    {
                        "type": "local_shell_call",
                        "call_id": item.get("id"),
                        "status": self._local_shell_status(item.get("status")),
                        "action": self._sanitize_local_shell_action(item),
                    }
                )
                continue
            if item_type == "webSearch":
                history.append(
                    {
                        "type": "web_search_call",
                        "status": "completed",
                        "action": self._sanitize_web_search_action(item),
                    }
                )
                continue
            history.append({"type": "other"})
        return history

    def _content_item_from_user_input(self, part: dict[str, Any]) -> dict[str, Any]:
        part_type = part.get("type")
        if part_type == "text":
            return {"type": "input_text", "text": part.get("text", "")}
        if part_type in {"image", "localImage"}:
            return {"type": "input_image", "image_url": part.get("url") or part.get("path") or ""}
        name = part.get("name") or part.get("path") or part.get("type") or "input"
        return {"type": "input_text", "text": str(name)}

    def _local_shell_status(self, status: Any) -> str:
        if status in {"completed", "success"}:
            return "completed"
        if status in {"inProgress", "running"}:
            return "in_progress"
        return "incomplete"

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
