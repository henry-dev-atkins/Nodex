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

MERGE_MODES = {"verbose", "summary", "decision", "analysis"}


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
    local_thread_id: str | None = None
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
        session.local_thread_id = thread["id"]
        session.thread_id = thread["id"]
        session.last_used_monotonic = asyncio.get_running_loop().time()
        async with self._session_lock:
            self.sessions[thread["id"]] = session
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
        thread = self.db.get_thread(thread_id)
        if not thread:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        async with self._session_lock:
            session = self.sessions.get(thread_id)
        if session:
            if thread.parentThreadId and thread.status == "error":
                await self._retire_session(session)
                return await self._resume_child_session_from_db(thread)
            session.last_used_monotonic = asyncio.get_running_loop().time()
            return session
        if thread.parentThreadId:
            return await self._resume_child_session_from_db(thread)
        session = await self._spawn_session()
        remote_thread_id = self._remote_thread_id(thread)
        result = await session.rpc.request_with_retry("thread/resume", self._thread_resume_params(remote_thread_id), timeout_s=60)
        thread = result["thread"]
        session.local_thread_id = thread_id
        session.thread_id = thread["id"]
        session.last_used_monotonic = asyncio.get_running_loop().time()
        if thread_id == thread["id"]:
            self._sync_thread_snapshot(thread, parent_thread_id=None, forked_from_turn_id=None)
        else:
            self._update_local_thread_from_codex(thread_id, thread)
        async with self._session_lock:
            self.sessions[thread_id] = session
        return session

    async def fork_thread(self, thread_id: str, title: str | None = None) -> ThreadRecord:
        parent_session = await self.get_or_resume_session(thread_id)
        parent_turn_id = self.db.get_last_turn_id(thread_id)
        result = await parent_session.rpc.request_with_retry(
            "thread/fork",
            {"threadId": parent_session.thread_id or thread_id, "persistExtendedHistory": True},
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
        resumed.local_thread_id = child_thread_id
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
        parent_thread = self.db.get_thread(thread_id)
        if not parent_thread:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        history = self._history_from_turn_snapshots(
            self._lineage_turn_snapshots(thread_id, upto_turn_id=turn_id, include_error_turns=False),
            include_tool_calls=False,
        )
        if not history:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "history_unavailable",
                        "message": "Cannot branch from this turn because no replayable history is available",
                        "details": {},
                    }
                },
            )
        child_session = await self._spawn_session()
        try:
            resumed = await child_session.rpc.request_with_retry(
                "thread/resume",
                self._thread_resume_params(self._remote_thread_id(parent_thread), history=history),
                timeout_s=60,
            )
        except TimeoutError as exc:
            await self._retire_session(child_session)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "code": "codex_rpc_timeout",
                        "message": "Timed out while creating a branch from this turn",
                        "details": {},
                    }
                },
            ) from exc
        except JsonRpcError as exc:
            await self._retire_session(child_session)
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
        child_thread = resumed["thread"]
        child_thread_id = child_thread["id"]
        child_session.local_thread_id = child_thread_id
        child_session.thread_id = child_thread_id
        child_session.last_used_monotonic = asyncio.get_running_loop().time()
        if not child_thread.get("turns"):
            try:
                read_result = await child_session.rpc.request_with_retry(
                    "thread/read",
                    {"threadId": child_thread_id, "includeTurns": True},
                    timeout_s=60,
                )
            except TimeoutError as exc:
                await self._retire_session(child_session)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {
                            "code": "codex_rpc_timeout",
                            "message": "Timed out while validating the new branch snapshot",
                            "details": {},
                        }
                    },
                ) from exc
            except JsonRpcError as exc:
                await self._retire_session(child_session)
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
            read_thread = read_result.get("thread")
            if isinstance(read_thread, dict):
                child_thread = read_thread
        if not child_thread.get("turns"):
            await self._retire_session(child_session)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "code": "branch_snapshot_empty",
                        "message": "Branch creation returned no replayable turns",
                        "details": {},
                    }
                },
            )
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
                    "threadId": session.thread_id or thread_id,
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

    async def interrupt_turn(self, thread_id: str) -> TurnRecord:
        session = await self.get_or_resume_session(thread_id)
        running_turn = None
        if session.active_turn_id:
            running_turn = self.db.get_turn(thread_id, session.active_turn_id)
        if running_turn is None:
            running_turn = next(
                (
                    turn
                    for turn in reversed(self.db.list_turns(thread_id))
                    if turn.status in {"running", "inProgress"}
                ),
                None,
            )
        if not running_turn:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "turn_not_running", "message": "No active turn to interrupt", "details": {}}},
            )
        try:
            await session.rpc.request_with_retry(
                "turn/interrupt",
                {
                    "threadId": session.thread_id or thread_id,
                    "turnId": running_turn.turnId,
                },
                timeout_s=30,
            )
        except JsonRpcError:
            pass
        session.active_turn_id = None
        session.pending_turn = None
        updated_turn = self.db.update_turn_status(
            thread_id,
            running_turn.turnId,
            "interrupted",
            completed_at=utc_now(),
            metadata={"interruptedByUser": True},
        ) or running_turn
        thread = self.db.update_thread_status(thread_id, "idle")
        await self.ws.emit_turn_updated(updated_turn)
        if thread:
            await self.ws.emit_thread_updated(thread)
        return updated_turn

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
        source_turn_id: str,
        dest_thread_id: str,
        dest_turn_id: str | None = None,
        merge_mode: str = "verbose",
    ) -> ImportPreviewRecord:
        await self.get_thread(source_thread_id)
        await self.get_thread(dest_thread_id)
        merge_mode = self._normalize_merge_mode(merge_mode)
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
        source_nodes = self._resolve_branch_scope(source_thread_id, source_turn_id)
        blob = await self._build_merge_transfer_blob(source_thread_id, source_turn_id, source_nodes, merge_mode)
        preview = ImportPreviewRecord(
            previewId=f"imp_prev_{uuid.uuid4().hex}",
            destThreadId=dest_thread_id,
            destTurnId=dest_turn_id,
            sourceThreadId=source_thread_id,
            sourceAnchorTurnId=source_turn_id,
            sourceNodes=source_nodes,
            mergeMode=merge_mode,
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
        session_key = session.local_thread_id or session.thread_id
        if session_key:
            async with self._session_lock:
                current = self.sessions.get(session_key)
                if current is session:
                    self.sessions.pop(session_key, None)
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
        remote_thread_id = self._extract_thread_id(params) or session.thread_id
        thread_id = session.local_thread_id or remote_thread_id
        if not thread_id:
            return
        if remote_thread_id:
            session.thread_id = remote_thread_id
        turn_id = self._extract_turn_id(params)
        seq_key = turn_id or "__thread__"
        seq = session.event_seq_by_turn.get(seq_key, 0) + 1
        session.event_seq_by_turn[seq_key] = seq
        event = self.db.append_event(thread_id, turn_id, seq, method, params)
        await self.ws.emit_event(event)
        session.last_used_monotonic = asyncio.get_running_loop().time()
        await self._apply_notification_side_effects(session, method, params)

    async def _apply_notification_side_effects(self, session: CodexSession, method: str, params: dict[str, Any]) -> None:
        thread_id = session.local_thread_id or self._extract_thread_id(params) or session.thread_id
        if not thread_id:
            return
        if method == "thread/started":
            if session.local_thread_id and session.thread_id and session.local_thread_id != session.thread_id:
                thread_record = self._update_local_thread_from_codex(session.local_thread_id, params["thread"])
            else:
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
            if turn_record:
                turn_record = self._persist_turn_items_from_events(turn_record)
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
        session.pending_approvals[approval_id] = ApprovalHandle(request_id=msg["id"], method=method, params=params)
        self.db.upsert_approval(approval)
        await self.ws.emit_approval_requested(approval)

    async def _handle_stderr(self, session: CodexSession, line: str) -> None:
        thread_id = session.local_thread_id or session.thread_id
        if not thread_id:
            return
        seq = session.event_seq_by_turn.get("__thread__", 0) + 1
        session.event_seq_by_turn["__thread__"] = seq
        event = self.db.append_event(thread_id, None, seq, "codex/stderr", {"line": line})
        await self.ws.emit_event(event)

    async def _handle_session_exit(self, session: CodexSession, code: int | None) -> None:
        local_thread_id = session.local_thread_id or session.thread_id
        if self._stopping or not local_thread_id or not session.thread_id:
            return
        async with self._session_lock:
            current = self.sessions.get(local_thread_id)
            if current is session:
                self.sessions.pop(local_thread_id, None)
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
                resumed.local_thread_id = local_thread_id
                resumed.thread_id = result["thread"]["id"]
                resumed.restart_attempted = True
                resumed.last_used_monotonic = asyncio.get_running_loop().time()
                if local_thread_id == resumed.thread_id:
                    self._sync_thread_snapshot(result["thread"], title=None)
                else:
                    self._update_local_thread_from_codex(local_thread_id, result["thread"])
                async with self._session_lock:
                    self.sessions[local_thread_id] = resumed
                thread = self.db.update_thread_status(local_thread_id, "idle", metadata={"lastRestartExitCode": code})
                if thread:
                    await self.ws.emit_thread_updated(thread)
                return
            except Exception as exc:
                thread = self.db.update_thread_status(local_thread_id, "dead", metadata={"restartError": str(exc), "lastExitCode": code})
                if thread:
                    await self.ws.emit_thread_updated(thread)
                return
        thread = self.db.update_thread_status(local_thread_id, "dead", metadata={"lastExitCode": code})
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
                "remoteThreadId": codex_thread.get("id"),
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

    def _thread_start_params(self, ephemeral: bool = False, persist_extended_history: bool = True) -> dict[str, Any]:
        return {
            "cwd": str(self.settings.workspace_dir),
            "approvalPolicy": self.settings.approval_policy,
            "ephemeral": ephemeral,
            "experimentalRawEvents": False,
            "persistExtendedHistory": persist_extended_history,
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

    def _remote_thread_id(self, thread: ThreadRecord) -> str:
        remote_thread_id = thread.metadata.get("remoteThreadId")
        if isinstance(remote_thread_id, str) and remote_thread_id:
            return remote_thread_id
        return thread.threadId

    def _update_local_thread_from_codex(self, local_thread_id: str, codex_thread: dict[str, Any]) -> ThreadRecord:
        existing = self.db.get_thread(local_thread_id)
        if not existing:
            return self._thread_record_from_codex(codex_thread)
        metadata = dict(existing.metadata)
        metadata.update(
            {
                "preview": codex_thread.get("preview"),
                "cwd": codex_thread.get("cwd"),
                "path": codex_thread.get("path"),
                "cliVersion": codex_thread.get("cliVersion"),
                "modelProvider": codex_thread.get("modelProvider"),
                "source": codex_thread.get("source"),
                "remoteThreadId": codex_thread.get("id"),
            }
        )
        updated = ThreadRecord(
            threadId=existing.threadId,
            title=codex_thread.get("name") or codex_thread.get("preview") or existing.title,
            createdAt=existing.createdAt,
            updatedAt=self._from_unix(codex_thread.get("updatedAt")),
            parentThreadId=existing.parentThreadId,
            forkedFromTurnId=existing.forkedFromTurnId,
            status=self._normalize_thread_status(codex_thread.get("status"), fallback=existing.status),
            metadata=metadata,
        )
        self.db.upsert_thread(updated)
        return updated

    def _lineage_turn_snapshots(self, thread_id: str, upto_turn_id: str | None = None, include_error_turns: bool = False) -> list[dict[str, Any]]:
        thread = self.db.get_thread(thread_id)
        if not thread:
            return []
        turns: list[dict[str, Any]] = []
        if thread.parentThreadId and thread.forkedFromTurnId:
            turns.extend(self._lineage_turn_snapshots(thread.parentThreadId, thread.forkedFromTurnId, include_error_turns=True))
        for turn in self.db.list_turns(thread_id):
            if turn.status in {"error", "running", "interrupted"} and not include_error_turns:
                if upto_turn_id and turn.turnId == upto_turn_id:
                    break
                continue
            turns.append({"id": turn.turnId, "items": self._items_for_history_from_turn(turn)})
            if upto_turn_id and turn.turnId == upto_turn_id:
                break
        return turns

    def _items_for_history_from_turn(self, turn: TurnRecord) -> list[dict[str, Any]]:
        fallback_text = (turn.userText or "").strip()
        existing = turn.metadata.get("items", [])
        if isinstance(existing, list):
            normalized_existing = [item for item in existing if isinstance(item, dict)]
            if normalized_existing:
                return self._ensure_user_message_item(normalized_existing, fallback_text)
        recovered = self._items_from_turn_events(turn.threadId, turn.turnId)
        if recovered:
            return self._ensure_user_message_item(recovered, fallback_text)
        if not fallback_text:
            return []
        return [self._user_message_item(fallback_text)]

    def _user_message_item(self, text: str) -> dict[str, Any]:
        return {
            "type": "userMessage",
            "content": [{"type": "text", "text": text, "text_elements": []}],
        }

    def _ensure_user_message_item(self, items: list[dict[str, Any]], fallback_text: str) -> list[dict[str, Any]]:
        if not fallback_text:
            return items
        for item in items:
            if item.get("type") != "userMessage":
                continue
            extracted = self._extract_user_text_from_items([item])
            if extracted.strip():
                return items
        return [self._user_message_item(fallback_text), *items]

    def _items_from_turn_events(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        index_by_id: dict[str, int] = {}
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type != "item/completed":
                continue
            item = event.payload.get("item")
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in index_by_id:
                items[index_by_id[item_id]] = item
                continue
            if isinstance(item_id, str):
                index_by_id[item_id] = len(items)
            items.append(item)
        return items

    def _persist_turn_items_from_events(self, turn: TurnRecord) -> TurnRecord:
        existing = turn.metadata.get("items", [])
        if isinstance(existing, list) and any(isinstance(item, dict) for item in existing):
            return turn
        recovered = self._items_from_turn_events(turn.threadId, turn.turnId)
        if not recovered:
            return turn
        metadata = dict(turn.metadata)
        metadata["items"] = recovered
        updated = self.db.update_turn_status(
            turn.threadId,
            turn.turnId,
            turn.status,
            completed_at=turn.completedAt,
            metadata=metadata,
        )
        return updated or turn

    def _history_from_turn_snapshots(
        self,
        turns: list[dict[str, Any]],
        include_tool_calls: bool = False,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for turn in turns:
            history.extend(
                self._response_items_from_thread_items(
                    turn.get("items", []),
                    include_tool_calls=include_tool_calls,
                )
            )
        return history

    async def _resume_child_session_from_db(self, thread: ThreadRecord) -> CodexSession:
        parent = self.db.get_thread(thread.parentThreadId or "")
        if not parent:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "thread_unavailable", "message": "Missing parent thread for branch resume", "details": {}}},
            )
        history = self._history_from_turn_snapshots(
            self._lineage_turn_snapshots(thread.threadId, include_error_turns=False),
            include_tool_calls=False,
        )
        session = await self._spawn_session()
        result = await session.rpc.request_with_retry(
            "thread/resume",
            self._thread_resume_params(self._remote_thread_id(parent), history=history),
            timeout_s=60,
        )
        session.local_thread_id = thread.threadId
        session.thread_id = result["thread"]["id"]
        session.last_used_monotonic = asyncio.get_running_loop().time()
        self._update_local_thread_from_codex(thread.threadId, result["thread"])
        async with self._session_lock:
            self.sessions[thread.threadId] = session
        return session

    def _approval_result(self, method: str, decision: str) -> dict[str, Any]:
        approve = decision == "approve"
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept" if approve else "decline"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept" if approve else "decline"}
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "approved" if approve else "denied"}
        return {"decision": "decline"}

    async def _build_merge_transfer_blob(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        if merge_mode == "verbose":
            return self._build_verbose_transfer_blob(source_thread_id, source_turn_id, source_nodes)
        prompt = self._build_condensed_merge_prompt(source_thread_id, source_turn_id, source_nodes, merge_mode)
        try:
            preview = await asyncio.wait_for(self._run_temporary_preview_prompt(prompt), timeout=18)
        except Exception:
            preview = self._build_condensed_merge_fallback(source_thread_id, source_turn_id, source_nodes, merge_mode)
        return preview.strip()

    def _build_transfer_blob(self, source_thread_id: str, source_turn_id: str | list[str]) -> str:
        anchor_turn_id = source_turn_id[-1] if isinstance(source_turn_id, list) else source_turn_id
        source_nodes = self._resolve_branch_scope(source_thread_id, anchor_turn_id)
        return self._build_verbose_transfer_blob(source_thread_id, anchor_turn_id, source_nodes)

    def _resolve_branch_scope(self, source_thread_id: str, anchor_turn_id: str) -> list[dict[str, str]]:
        thread = self.db.get_thread(source_thread_id)
        anchor_turn = self.db.get_turn(source_thread_id, anchor_turn_id)
        if not thread or not anchor_turn:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "turn_not_found", "message": f"Unknown source turn: {anchor_turn_id}", "details": {}}},
            )
        ordered: list[dict[str, str]] = []
        if thread.parentThreadId and thread.forkedFromTurnId:
            ordered.extend(self._resolve_branch_scope(thread.parentThreadId, thread.forkedFromTurnId))
        turns = self.db.list_turns(source_thread_id)
        for turn in turns:
            if turn.idx <= anchor_turn.idx:
                ordered.append({"threadId": source_thread_id, "turnId": turn.turnId})
        deduped: list[dict[str, str]] = []
        seen = set()
        for node in ordered:
            key = (node["threadId"], node["turnId"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(node)
        return deduped

    def _build_verbose_transfer_blob(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
    ) -> str:
        source_thread = self.db.get_thread(source_thread_id)
        source_label = source_thread.title if source_thread and source_thread.title else source_thread_id
        lines = [
            "Copied branch context",
            "",
            f"Source branch: {source_label}",
            f"Source thread ID: {source_thread_id}",
            f"Source anchor turn ID: {source_turn_id}",
            f"Selected turns: {len(source_nodes)}",
            "",
        ]
        seen_summaries = set()
        seen_results = set()
        for node in source_nodes:
            thread_id = node["threadId"]
            turn = self.db.get_turn(thread_id, node["turnId"])
            if not turn:
                continue
            final_message = self._extract_final_agent_text(thread_id, turn.turnId)
            reasoning_summary = self._extract_reasoning_summary(thread_id, turn.turnId)
            decision_summary = self._extract_decision_summary(thread_id, turn.turnId, turn.status)
            if final_message:
                result_text = final_message
            else:
                result_text = "No final assistant result captured yet."
            summary_text = reasoning_summary or final_message or turn.userText
            normalized_summary = self._normalize_merge_block(summary_text)
            normalized_result = self._normalize_merge_block(result_text)
            command_summaries = self._extract_command_summaries(thread_id, turn.turnId)
            lines.append(f"{self._merge_branch_label(thread_id)} / Turn {turn.idx} ({turn.turnId})")
            lines.append("Prompt:")
            lines.append(turn.userText)
            lines.append("")
            if normalized_summary not in seen_summaries:
                lines.append("Summary:")
                lines.append(summary_text)
                lines.append("")
                seen_summaries.add(normalized_summary)
            lines.append("Decision:")
            lines.append(decision_summary)
            lines.append("")
            if normalized_result not in seen_results:
                lines.append("Result:")
                lines.append(result_text)
                seen_results.add(normalized_result)
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
        next_links.append(
            {
                "kind": "contextImport",
                "mergeMode": preview.mergeMode,
                "sourceThreadId": preview.sourceThreadId,
                "sourceTurnId": preview.sourceAnchorTurnId,
                "sourceAnchorTurnId": preview.sourceAnchorTurnId,
                "sourceNodes": preview.sourceNodes,
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

    def _normalize_merge_mode(self, merge_mode: str | None) -> str:
        normalized = str(merge_mode or "verbose").strip().lower()
        if normalized not in MERGE_MODES:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_request", "message": f"Unsupported merge mode: {merge_mode}", "details": {}}},
            )
        return normalized

    def _merge_branch_label(self, thread_id: str) -> str:
        thread = self.db.get_thread(thread_id)
        return thread.title if thread and thread.title else thread_id

    def _normalize_merge_block(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).lower()

    def _build_merge_scope_notes(self, source_nodes: list[dict[str, str]]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        seen_summary_blocks = set()
        seen_result_blocks = set()
        for node in source_nodes:
            thread_id = node["threadId"]
            turn = self.db.get_turn(thread_id, node["turnId"])
            if not turn:
                continue
            final_message = self._extract_final_agent_text(thread_id, turn.turnId) or "No final assistant result captured yet."
            reasoning_summary = self._extract_reasoning_summary(thread_id, turn.turnId)
            summary_text = reasoning_summary or final_message or turn.userText
            decision_summary = self._extract_decision_summary(thread_id, turn.turnId, turn.status)
            command_summaries = self._extract_command_summaries(thread_id, turn.turnId)
            summary_text = summary_text if self._normalize_merge_block(summary_text) not in seen_summary_blocks else ""
            result_text = final_message if self._normalize_merge_block(final_message) not in seen_result_blocks else ""
            if summary_text:
                seen_summary_blocks.add(self._normalize_merge_block(summary_text))
            if result_text:
                seen_result_blocks.add(self._normalize_merge_block(result_text))
            notes.append(
                {
                    "threadId": thread_id,
                    "turnId": turn.turnId,
                    "turnIdx": turn.idx,
                    "branchLabel": self._merge_branch_label(thread_id),
                    "prompt": turn.userText,
                    "summary": summary_text,
                    "decision": decision_summary,
                    "result": result_text,
                    "commands": command_summaries,
                }
            )
        return notes

    def _build_condensed_merge_prompt(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        notes = self._build_merge_scope_notes(source_nodes)
        length_instruction = {
            "summary": "Write exactly 4 sentences.",
            "decision": "Write exactly 2 sentences.",
            "analysis": "Write one short paragraph.",
        }[merge_mode]
        purpose_instruction = {
            "summary": "Summarize the branch for reuse in another branch without losing the important facts and conclusions.",
            "decision": "State the branch-level decision centered on the selected turn's final state, including the important rationale.",
            "analysis": "Provide a concise analytical synthesis of the branch, preserving rationale, tradeoffs, and the current conclusion.",
        }[merge_mode]
        lines = [
            "You are condensing branch context so it can be merged into another branch.",
            "Respond with plain text only.",
            "Do not use tools, file changes, web searches, or approvals.",
            "Do not mention that this is a summary or copied context.",
            "Use only the material provided below.",
            "Focus on substantive facts, conclusions, decisions, and rationale.",
            "Ignore assistant process narration, planning chatter, and workflow bookkeeping unless it is itself the substantive outcome.",
            length_instruction,
            purpose_instruction,
            "",
            f"Selected source branch: {self._merge_branch_label(source_thread_id)}",
            f"Selected source thread ID: {source_thread_id}",
            f"Selected anchor turn ID: {source_turn_id}",
            f"Contributing turns: {len(notes)}",
            "",
            "Branch material:",
            "",
        ]
        for note in notes:
            lines.extend(
                [
                    f"{note['branchLabel']} / T{note['turnIdx']} ({note['threadId']}:{note['turnId']})",
                    "Prompt:",
                    note["prompt"],
                ]
            )
            if note["summary"]:
                lines.extend(["Summary:", note["summary"]])
            lines.extend(["Decision:", note["decision"]])
            if note["result"]:
                lines.extend(["Result:", note["result"]])
            if note["commands"]:
                lines.append("Commands:")
                lines.extend(f"- {command}" for command in note["commands"])
            lines.append("")
        return "\n".join(lines).strip()

    def _build_condensed_merge_fallback(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        notes = self._build_merge_scope_notes(source_nodes)
        if not notes:
            return "No branch context was available to merge."
        final_note = notes[-1]
        if merge_mode == "decision":
            rationale = final_note["summary"] or final_note["result"] or final_note["prompt"]
            return f"{final_note['decision']} {self._truncate_merge_text(rationale, 220)}".strip()
        if merge_mode == "summary":
            sentences = []
            for note in notes:
                for candidate in [note["summary"], note["result"], note["decision"]]:
                    if candidate:
                        sentences.append(candidate.strip())
                if len(sentences) >= 4:
                    break
            return " ".join(sentences[:4]).strip()
        summary = final_note["summary"] or final_note["result"] or final_note["prompt"]
        return f"{summary} {final_note['decision']}".strip()

    def _truncate_merge_text(self, text: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: max(limit - 3, 1)].rstrip()}..."

    async def _run_temporary_preview_prompt(self, prompt: str) -> str:
        command = resolve_subprocess_command(split_command(self.settings.codex_bin) + ["app-server"])
        completion_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        chunks: list[str] = []
        completed_messages: list[str] = []
        rpc_holder: dict[str, CodexRpcClient] = {}
        thread_id_holder: dict[str, str] = {}

        async def notification_handler(msg: dict[str, Any]) -> None:
            method = msg.get("method")
            params = msg.get("params", {})
            if method == "thread/started":
                thread = params.get("thread", {})
                if thread.get("id"):
                    thread_id_holder["thread_id"] = str(thread["id"])
                return
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if delta:
                    chunks.append(str(delta))
                return
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    text = self._extract_message_item_text(item)
                    if text:
                        completed_messages.append(text)
                return
            if method == "turn/completed" and not completion_future.done():
                result = completed_messages[-1] if completed_messages else "".join(chunks).strip()
                completion_future.set_result(result)
                return
            if method == "error" and not completion_future.done():
                error = params.get("error", {})
                completion_future.set_exception(RuntimeError(str(error.get("message") or "Preview generation failed")))

        async def server_request_handler(msg: dict[str, Any]) -> None:
            method = msg.get("method")
            rpc = rpc_holder["rpc"]
            await rpc.send_response(
                msg.get("id"),
                result=self._approval_result(method, "deny") if method in APPROVAL_METHODS else None,
                error=None if method in APPROVAL_METHODS else {"code": -32601, "message": f"Unsupported server request: {method}"},
            )

        async def stderr_handler(_line: str) -> None:
            return None

        async def exit_handler(code: int | None) -> None:
            if not completion_future.done():
                completion_future.set_exception(RuntimeError(f"Preview Codex session exited early: {code}"))

        rpc = await CodexRpcClient.start(
            command=command,
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            stderr_handler=stderr_handler,
            exit_handler=exit_handler,
        )
        rpc_holder["rpc"] = rpc
        try:
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": APP_NAME, "title": "Codex UI Wrapper", "version": APP_VERSION},
                    "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
                },
                timeout_s=30,
            )
            await rpc.notify("initialized")
            started = await rpc.request_with_retry(
                "thread/start",
                self._thread_start_params(ephemeral=True, persist_extended_history=False),
                timeout_s=60,
            )
            thread = started.get("thread", {})
            thread_id = str(thread.get("id") or thread_id_holder.get("thread_id") or "")
            if not thread_id:
                raise RuntimeError("Preview Codex session did not start a thread")
            started_turn = await rpc.request_with_retry(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                },
                timeout_s=600,
            )
            turn_id = str(started_turn.get("turn", {}).get("id", ""))
            for _ in range(120):
                snapshot = await rpc.request_with_retry(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": True},
                    timeout_s=60,
                )
                turns = snapshot.get("thread", {}).get("turns", [])
                current_turn = next((turn for turn in turns if str(turn.get("id")) == turn_id), None)
                if current_turn and str(current_turn.get("status")) not in {"inProgress", "running"}:
                    result = self._extract_preview_text_from_items(current_turn.get("items", []))
                    if result:
                        return result.strip()
                    break
                if completion_future.done():
                    result = completion_future.result()
                    if result:
                        return result.strip()
                await asyncio.sleep(1)
            result = completed_messages[-1] if completed_messages else "".join(chunks).strip()
            if result:
                return result.strip()
            raise RuntimeError("Preview Codex session did not produce assistant output")
        finally:
            await rpc.close()

    def _extract_message_item_text(self, item: dict[str, Any]) -> str:
        text = str(item.get("text", "")).strip()
        if text:
            return text
        content = item.get("content", [])
        if isinstance(content, list):
            joined = "\n".join(
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("text")
            ).strip()
            if joined:
                return joined
        return ""

    def _extract_preview_text_from_items(self, items: list[dict[str, Any]]) -> str:
        messages = [
            self._extract_message_item_text(item)
            for item in items
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        messages = [message for message in messages if message]
        return messages[-1] if messages else ""

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

    def _build_response_history(
        self,
        codex_thread: dict[str, Any],
        turn_id: str,
        include_tool_calls: bool = False,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        found = False
        for turn in codex_thread.get("turns", []):
            history.extend(
                self._response_items_from_thread_items(
                    turn.get("items", []),
                    include_tool_calls=include_tool_calls,
                )
            )
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
        raw_action = item.get("action") if isinstance(item.get("action"), dict) else item
        command_value = raw_action.get("command")
        if isinstance(command_value, list):
            command = [str(part) for part in command_value if part is not None]
        else:
            command = split_command(str(command_value or item.get("command", "")))
        action: dict[str, Any] = {
            "type": "exec",
            "command": command,
        }
        working_directory = raw_action.get("cwd") or raw_action.get("working_directory") or item.get("cwd") or item.get("working_directory")
        if working_directory is not None:
            action["working_directory"] = str(working_directory)
        timeout_ms = raw_action.get("timeout_ms", item.get("timeout_ms"))
        if isinstance(timeout_ms, int) and timeout_ms >= 0:
            action["timeout_ms"] = timeout_ms
        user = raw_action.get("user", item.get("user"))
        if user is not None:
            action["user"] = str(user)
        env = raw_action.get("env", item.get("env"))
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

    def _response_items_from_thread_items(
        self,
        items: list[dict[str, Any]],
        include_tool_calls: bool = True,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for item in items:
            item_type = item.get("type")
            if item_type == "message":
                history.append(
                    self._sanitize_message_history_item(
                        str(item.get("role", "assistant")),
                        [part for part in item.get("content", []) if isinstance(part, dict)],
                        phase=item.get("phase"),
                    )
                )
                continue
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
            if item_type in {"commandExecution", "local_shell_call", "localShellCall"}:
                if not include_tool_calls:
                    continue
                history.append(
                    {
                        "type": "local_shell_call",
                        "call_id": item.get("id") or item.get("call_id"),
                        "status": self._local_shell_status(item.get("status")),
                        "action": self._sanitize_local_shell_action(item),
                    }
                )
                continue
            if item_type in {"webSearch", "web_search_call"}:
                if not include_tool_calls:
                    continue
                history.append(
                    {
                        "type": "web_search_call",
                        "status": self._local_shell_status(item.get("status")) if item_type == "web_search_call" else "completed",
                        "action": self._sanitize_web_search_action(item),
                    }
                )
                continue
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
