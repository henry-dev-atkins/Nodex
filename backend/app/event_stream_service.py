from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .db import Database
from .ws import WebSocketHub


ExtractThreadIdFn = Callable[[dict[str, Any]], str | None]
ExtractTurnIdFn = Callable[[dict[str, Any]], str | None]
ApplyNotificationSideEffectsFn = Callable[[Any, str, dict[str, Any]], Awaitable[None]]
MonotonicTimeFn = Callable[[], float]


class EventStreamService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        extract_thread_id: ExtractThreadIdFn,
        extract_turn_id: ExtractTurnIdFn,
        apply_notification_side_effects: ApplyNotificationSideEffectsFn,
        monotonic_time: MonotonicTimeFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._extract_thread_id = extract_thread_id
        self._extract_turn_id = extract_turn_id
        self._apply_notification_side_effects = apply_notification_side_effects
        self._monotonic_time = monotonic_time

    async def handle_notification(self, session: Any, msg: dict[str, Any]) -> None:
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
        session.last_used_monotonic = self._monotonic_time()
        await self._apply_notification_side_effects(session, method, params)

    async def handle_stderr(self, session: Any, line: str) -> None:
        thread_id = session.local_thread_id or session.thread_id
        if not thread_id:
            return
        seq = session.event_seq_by_turn.get("__thread__", 0) + 1
        session.event_seq_by_turn["__thread__"] = seq
        event = self.db.append_event(thread_id, None, seq, "codex/stderr", {"line": line})
        await self.ws.emit_event(event)
