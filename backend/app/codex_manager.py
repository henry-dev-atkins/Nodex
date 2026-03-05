from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from .approval_service import ApprovalService, approval_result_for_method
from .branching_service import BranchingService
from .codex_rpc import CodexRpcClient
from .conversation_service import ConversationService
from .db import Database
from .event_stream_service import EventStreamService
from .import_service import ImportService
from .lifecycle_service import LifecycleService
from .maintenance_service import MaintenanceService
from .merge_context_service import MergeContextService
from .models import ApprovalRecord, ImportPreviewRecord, ThreadRecord, TurnRecord
from .notification_effects import NotificationEffectsService
from .response_history import ResponseHistoryProjector
from .schema_contract_service import SchemaContractService
from .session_policy import select_session_for_capacity_retirement
from .session_recovery import SessionRecoveryService
from .session_runtime import start_initialized_rpc
from .settings import Settings
from .temporary_preview_service import TemporaryPreviewService
from .thread_params_service import ThreadParamsService
from .thread_snapshot_service import ThreadSnapshotService
from .turn_history import TurnHistoryService
from .turn_record_service import TurnRecordService
from .turn_execution_service import TurnExecutionService
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
        self._maintenance = MaintenanceService(
            db=db,
            sessions=self.sessions,
            session_lock=self._session_lock,
            session_idle_ttl_s=self.settings.session_idle_ttl_s,
        )
        self._response_history = ResponseHistoryProjector()
        self._turn_history = TurnHistoryService(db, self._response_history)
        self._merge_context = MergeContextService(db=db, now_iso=utc_now)
        self._thread_snapshots = ThreadSnapshotService(
            db=db,
            extract_user_text_from_items=self._response_history.extract_user_text_from_items,
            now_iso=utc_now,
        )
        self._thread_params = ThreadParamsService(
            workspace_dir=str(self.settings.workspace_dir),
            approval_policy=self.settings.approval_policy,
            service_name=APP_NAME,
        )
        self._schema_contracts = SchemaContractService(self.settings.schema_cache_dir)
        self._turn_records = TurnRecordService(
            db=db,
            normalize_turn_status=self._normalize_turn_status,
            now_iso=utc_now,
        )
        self._conversations = ConversationService(
            db=db,
            ws=ws,
            sessions=self.sessions,
            session_lock=self._session_lock,
            ensure_thread=self.get_thread,
            retire_session=lambda session: self._retire_session(session),
        )
        self._imports = ImportService(
            db=db,
            ws=ws,
            ensure_thread=self.get_thread,
            normalize_merge_mode=self._merge_context.normalize_merge_mode,
            resolve_branch_scope=self._merge_context.resolve_branch_scope,
            build_merge_transfer_blob=self._build_merge_transfer_blob,
            detect_suspected_secrets=self._merge_context.detect_suspected_secrets,
            plus_seconds=self._plus_seconds,
            branch_from_turn=lambda thread_id, turn_id: self.branch_from_turn(thread_id, turn_id, title=None),
            start_turn=lambda thread_id, text: self.start_turn(thread_id, text),
            annotate_imported_turn=self._merge_context.annotate_imported_turn,
            import_preview_ttl_s=self.settings.import_preview_ttl_s,
        )
        self._lifecycle = LifecycleService(
            db=db,
            ws=ws,
            sessions=self.sessions,
            session_lock=self._session_lock,
            spawn_session=lambda: self._spawn_session(),
            retire_session=lambda session: self._retire_session(session),
            thread_start_params=self._thread_params.thread_start_params,
            thread_resume_params=lambda thread_id, history: self._thread_params.thread_resume_params(thread_id, history=history),
            thread_record_from_codex=lambda thread, title: self._thread_snapshots.thread_record_from_codex(thread, title=title),
            sync_thread_snapshot=lambda thread, parent_thread_id, forked_from_turn_id, title: self._sync_thread_snapshot(
                thread,
                parent_thread_id=parent_thread_id,
                forked_from_turn_id=forked_from_turn_id,
                title=title,
            ),
            update_local_thread_from_codex=self._thread_snapshots.update_local_thread_from_codex,
            remote_thread_id=self._thread_snapshots.remote_thread_id,
            lineage_turn_snapshots=lambda thread_id, upto_turn_id, include_error_turns: self._turn_history.lineage_turn_snapshots(
                thread_id,
                upto_turn_id=upto_turn_id,
                include_error_turns=include_error_turns,
            ),
            history_from_turn_snapshots=lambda turns, include_tool_calls: self._turn_history.history_from_turn_snapshots(
                turns,
                include_tool_calls=include_tool_calls,
            ),
            monotonic_time=lambda: asyncio.get_running_loop().time(),
        )
        self._turn_execution = TurnExecutionService(
            db=db,
            ws=ws,
            get_or_resume_session=lambda thread_id: self.get_or_resume_session(thread_id),
            ensure_turn_record=self._turn_records.ensure_turn_record,
            make_pending_turn=lambda idx, user_text: PendingTurn(idx, user_text),
            monotonic_time=lambda: asyncio.get_running_loop().time(),
            now_iso=utc_now,
        )
        self._branching = BranchingService(
            db=db,
            ws=ws,
            sessions=self.sessions,
            session_lock=self._session_lock,
            get_or_resume_session=lambda thread_id: self.get_or_resume_session(thread_id),
            spawn_session=lambda: self._spawn_session(),
            retire_session=lambda session: self._retire_session(session),
            thread_resume_params=lambda thread_id, history: self._thread_params.thread_resume_params(thread_id, history=history),
            remote_thread_id=self._thread_snapshots.remote_thread_id,
            sync_thread_snapshot=lambda thread, parent_thread_id, forked_from_turn_id, title: self._sync_thread_snapshot(
                thread,
                parent_thread_id=parent_thread_id,
                forked_from_turn_id=forked_from_turn_id,
                title=title,
            ),
            lineage_turn_snapshots=lambda thread_id, upto_turn_id, include_error_turns: self._turn_history.lineage_turn_snapshots(
                thread_id,
                upto_turn_id=upto_turn_id,
                include_error_turns=include_error_turns,
            ),
            history_from_turn_snapshots=lambda turns, include_tool_calls: self._turn_history.history_from_turn_snapshots(
                turns,
                include_tool_calls=include_tool_calls,
            ),
            monotonic_time=lambda: asyncio.get_running_loop().time(),
        )
        self._approvals = ApprovalService(
            db=db,
            ws=ws,
            sessions=self.sessions,
            session_lock=self._session_lock,
            approval_methods=APPROVAL_METHODS,
            make_approval_handle=lambda request_id, method, params: ApprovalHandle(request_id=request_id, method=method, params=params),
            approval_result=approval_result_for_method,
        )
        self._notification_effects = NotificationEffectsService(
            db=db,
            ws=ws,
            extract_thread_id=self._extract_thread_id,
            normalize_thread_status=self._normalize_thread_status,
            normalize_turn_status=self._normalize_turn_status,
            ensure_turn_record=self._turn_records.ensure_turn_record,
            persist_turn_items_from_events=self._turn_history.persist_turn_items_from_events,
            sync_thread_snapshot=lambda codex_thread: self._sync_thread_snapshot(codex_thread, title=None),
            update_local_thread_from_codex=self._thread_snapshots.update_local_thread_from_codex,
            make_pending_turn=lambda idx, user_text: PendingTurn(idx, user_text),
        )
        self._event_stream = EventStreamService(
            db=db,
            ws=ws,
            extract_thread_id=self._extract_thread_id,
            extract_turn_id=self._extract_turn_id,
            apply_notification_side_effects=lambda session, method, params: self._apply_notification_side_effects(session, method, params),
            monotonic_time=lambda: asyncio.get_running_loop().time(),
        )
        self._session_recovery = SessionRecoveryService(
            db=db,
            ws=ws,
            sessions=self.sessions,
            session_lock=self._session_lock,
            spawn_session=self._spawn_session,
            thread_resume_params=lambda thread_id: self._thread_params.thread_resume_params(thread_id),
            sync_thread_snapshot=lambda codex_thread: self._sync_thread_snapshot(codex_thread, title=None),
            update_local_thread_from_codex=self._thread_snapshots.update_local_thread_from_codex,
            monotonic_time=lambda: asyncio.get_running_loop().time(),
        )
        self._temporary_preview = TemporaryPreviewService(
            codex_bin=self.settings.codex_bin,
            approval_methods=APPROVAL_METHODS,
            thread_start_params=lambda: self._thread_params.thread_start_params(ephemeral=True, persist_extended_history=False),
            approval_result=self._approval_result,
            extract_message_item_text=self._merge_context.extract_message_item_text,
            extract_preview_text_from_items=self._merge_context.extract_preview_text_from_items,
        )
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
        self._schema_contracts.verify_schema_files()

    async def start_thread(self, title: str | None = None) -> ThreadRecord:
        return await self._lifecycle.start_thread(title=title)

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
        return await self._lifecycle.get_or_resume_session(thread_id)

    async def fork_thread(self, thread_id: str, title: str | None = None) -> ThreadRecord:
        return await self._branching.fork_thread(thread_id, title=title)

    async def branch_from_turn(self, thread_id: str, turn_id: str, title: str | None = None) -> ThreadRecord:
        return await self._branching.branch_from_turn(thread_id, turn_id, title=title)

    async def delete_conversation(self, thread_id: str) -> dict[str, Any]:
        return await self._conversations.delete_conversation(thread_id)

    async def rename_thread(self, thread_id: str, title: str) -> ThreadRecord:
        return await self._conversations.rename_thread(thread_id, title)

    async def delete_branch(self, thread_id: str) -> dict[str, Any]:
        return await self._conversations.delete_branch(thread_id)

    async def start_turn(self, thread_id: str, text: str) -> TurnRecord:
        return await self._turn_execution.start_turn(thread_id, text)

    async def interrupt_turn(self, thread_id: str) -> TurnRecord:
        return await self._turn_execution.interrupt_turn(thread_id)

    async def respond_approval(self, approval_id: str, decision: str) -> ApprovalRecord:
        return await self._approvals.respond_approval(approval_id, decision)

    async def create_import_preview(
        self,
        source_thread_id: str,
        source_turn_id: str,
        dest_thread_id: str,
        dest_turn_id: str | None = None,
        merge_mode: str = "verbose",
    ) -> ImportPreviewRecord:
        return await self._imports.create_import_preview(
            source_thread_id,
            source_turn_id,
            dest_thread_id,
            dest_turn_id=dest_turn_id,
            merge_mode=merge_mode,
        )

    async def commit_import_preview(self, preview_id: str, confirmed: bool, edited_transfer_blob: str) -> dict[str, Any]:
        return await self._imports.commit_import_preview(preview_id, confirmed, edited_transfer_blob)

    async def housekeeping_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(30)
            await self._maintenance.housekeeping_step(utc_now(), asyncio.get_running_loop().time())

    async def close(self) -> None:
        self._stopping = True
        await self._maintenance.close_sessions()

    async def _evict_idle_sessions(self) -> None:
        await self._maintenance.evict_idle_sessions(asyncio.get_running_loop().time())

    async def _retire_session(self, session: CodexSession) -> None:
        await self._maintenance.retire_session(session)

    async def _spawn_session(self) -> CodexSession:
        await self._ensure_capacity()
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

        rpc = await start_initialized_rpc(
            self.settings.codex_bin,
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            stderr_handler=stderr_handler,
            exit_handler=exit_handler,
            app_name=APP_NAME,
            app_version=APP_VERSION,
        )
        session = CodexSession(process_key=process_key, rpc=rpc, last_used_monotonic=asyncio.get_running_loop().time())
        session_ref["session"] = session
        return session

    async def _ensure_capacity(self) -> None:
        async with self._session_lock:
            sessions = list(self.sessions.values())
        capacity_reached, retirement_candidate = select_session_for_capacity_retirement(sessions, self.settings.session_limit)
        if not capacity_reached:
            return
        if retirement_candidate is None:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "codex_process_unavailable", "message": "All Codex sessions are busy", "details": {}}},
            )
        await self._retire_session(retirement_candidate)

    async def _handle_notification(self, session: CodexSession, msg: dict[str, Any]) -> None:
        await self._event_stream.handle_notification(session, msg)

    async def _apply_notification_side_effects(self, session: CodexSession, method: str, params: dict[str, Any]) -> None:
        await self._notification_effects.apply(session, method, params)

    async def _handle_server_request(self, session: CodexSession, msg: dict[str, Any]) -> None:
        await self._approvals.handle_server_request(session, msg)

    async def _handle_stderr(self, session: CodexSession, line: str) -> None:
        await self._event_stream.handle_stderr(session, line)

    async def _handle_session_exit(self, session: CodexSession, code: int | None) -> None:
        await self._session_recovery.handle_exit(session, code, stopping=self._stopping)

    def _sync_thread_snapshot(
        self,
        codex_thread: dict[str, Any],
        parent_thread_id: str | None = None,
        forked_from_turn_id: str | None = None,
        title: str | None = None,
    ) -> ThreadRecord:
        return self._thread_snapshots.sync_thread_snapshot(
            codex_thread,
            parent_thread_id=parent_thread_id,
            forked_from_turn_id=forked_from_turn_id,
            title=title,
        )

    def _normalize_thread_status(self, status: Any, fallback: str = "idle") -> str:
        return self._thread_snapshots.normalize_thread_status(status, fallback=fallback)

    def _normalize_turn_status(self, status: Any, fallback: str = "running") -> str:
        return self._thread_snapshots.normalize_turn_status(status, fallback=fallback)

    def _approval_result(self, method: str, decision: str) -> dict[str, Any]:
        return approval_result_for_method(method, decision)

    async def _build_merge_transfer_blob(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        if merge_mode == "verbose":
            return self._merge_context.build_verbose_transfer_blob(source_thread_id, source_turn_id, source_nodes)
        prompt = self._merge_context.build_condensed_merge_prompt(
            source_thread_id,
            source_turn_id,
            source_nodes,
            merge_mode,
        )
        try:
            preview = await asyncio.wait_for(self._run_temporary_preview_prompt(prompt), timeout=18)
        except Exception:
            preview = self._merge_context.build_condensed_merge_fallback(
                source_thread_id,
                source_turn_id,
                source_nodes,
                merge_mode,
            )
        return preview.strip()

    def _build_transfer_blob(self, source_thread_id: str, source_turn_id: str | list[str]) -> str:
        return self._merge_context.build_transfer_blob(source_thread_id, source_turn_id)

    async def _run_temporary_preview_prompt(self, prompt: str) -> str:
        return await self._temporary_preview.run_temporary_preview_prompt(prompt)

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

    def _plus_seconds(self, seconds: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
