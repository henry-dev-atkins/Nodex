from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .db import Database
from .models import ThreadRecord, TurnRecord
from .util import utc_now
from .ws import WebSocketHub


ExtractThreadIdFn = Callable[[dict[str, Any]], str | None]
NormalizeThreadStatusFn = Callable[[Any], str]
NormalizeTurnStatusFn = Callable[[Any, str], str]
EnsureTurnRecordFn = Callable[[str, str, str, Any], TurnRecord]
PersistTurnItemsFn = Callable[[TurnRecord], TurnRecord]
SyncThreadSnapshotFn = Callable[[dict[str, Any]], ThreadRecord]
UpdateLocalThreadFromCodexFn = Callable[[str, dict[str, Any]], ThreadRecord]
MakePendingTurnFn = Callable[[int, str], Any]


class NotificationEffectsService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        extract_thread_id: ExtractThreadIdFn,
        normalize_thread_status: NormalizeThreadStatusFn,
        normalize_turn_status: NormalizeTurnStatusFn,
        ensure_turn_record: EnsureTurnRecordFn,
        persist_turn_items_from_events: PersistTurnItemsFn,
        sync_thread_snapshot: SyncThreadSnapshotFn,
        update_local_thread_from_codex: UpdateLocalThreadFromCodexFn,
        make_pending_turn: MakePendingTurnFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._extract_thread_id = extract_thread_id
        self._normalize_thread_status = normalize_thread_status
        self._normalize_turn_status = normalize_turn_status
        self._ensure_turn_record = ensure_turn_record
        self._persist_turn_items_from_events = persist_turn_items_from_events
        self._sync_thread_snapshot = sync_thread_snapshot
        self._update_local_thread_from_codex = update_local_thread_from_codex
        self._make_pending_turn = make_pending_turn

    async def apply(self, session: Any, method: str, params: dict[str, Any]) -> None:
        thread_id = session.local_thread_id or self._extract_thread_id(params) or session.thread_id
        if not thread_id:
            return
        if method == "thread/started":
            if session.local_thread_id and session.thread_id and session.local_thread_id != session.thread_id:
                thread_record = self._update_local_thread_from_codex(session.local_thread_id, params["thread"])
            else:
                thread_record = self._sync_thread_snapshot(params["thread"])
            await self.ws.emit_thread_updated(thread_record)
            return
        if method == "thread/status/changed":
            thread = self.db.update_thread_status(thread_id, self._normalize_thread_status(params.get("status")))
            if thread:
                await self.ws.emit_thread_updated(thread)
            return
        if method == "turn/started":
            turn = params["turn"]
            pending = session.pending_turn or self._make_pending_turn(self.db.get_next_turn_index(thread_id), "")
            turn_record = self._ensure_turn_record(thread_id, turn["id"], turn.get("status", "running"), pending)
            session.active_turn_id = turn_record.turnId
            await self.ws.emit_turn_updated(turn_record)
            return
        if method == "turn/completed":
            turn = params["turn"]
            turn_record = self.db.update_turn_status(
                thread_id,
                turn["id"],
                self._normalize_turn_status(turn.get("status"), "completed"),
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
