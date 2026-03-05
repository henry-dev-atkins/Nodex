from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from .codex_rpc import JsonRpcError
from .db import Database
from .models import TurnRecord
from .ws import WebSocketHub


GetOrResumeSessionFn = Callable[[str], Awaitable[Any]]
EnsureTurnRecordFn = Callable[[str, str, str, Any], TurnRecord]
MakePendingTurnFn = Callable[[int, str], Any]
MonotonicTimeFn = Callable[[], float]
NowIsoFn = Callable[[], str]


class TurnExecutionService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        get_or_resume_session: GetOrResumeSessionFn,
        ensure_turn_record: EnsureTurnRecordFn,
        make_pending_turn: MakePendingTurnFn,
        monotonic_time: MonotonicTimeFn,
        now_iso: NowIsoFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._get_or_resume_session = get_or_resume_session
        self._ensure_turn_record = ensure_turn_record
        self._make_pending_turn = make_pending_turn
        self._monotonic_time = monotonic_time
        self._now_iso = now_iso

    async def start_turn(self, thread_id: str, text: str) -> TurnRecord:
        session = await self._get_or_resume_session(thread_id)
        if session.active_turn_id or session.pending_turn:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "turn_in_progress", "message": "Thread already has an active turn", "details": {}}},
            )
        pending = self._make_pending_turn(self.db.get_next_turn_index(thread_id), text)
        session.pending_turn = pending
        session.last_used_monotonic = self._monotonic_time()
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
        session = await self._get_or_resume_session(thread_id)
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
            completed_at=self._now_iso(),
            metadata={"interruptedByUser": True},
        ) or running_turn
        thread = self.db.update_thread_status(thread_id, "idle")
        await self.ws.emit_turn_updated(updated_turn)
        if thread:
            await self.ws.emit_thread_updated(thread)
        return updated_turn
