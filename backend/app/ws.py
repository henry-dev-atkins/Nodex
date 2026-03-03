from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .db import Database
from .models import ApprovalRecord, EventRecord, ThreadRecord, TurnRecord
from .util import utc_now


def _as_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    return value


@dataclass
class WebSocketHub:
    clients: set[WebSocket] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(websocket)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self.clients)
        dead: list[WebSocket] = []
        for websocket in sockets:
            try:
                await self.send_json(websocket, payload)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self.clients.discard(websocket)

    async def send_initial_snapshot(self, websocket: WebSocket, db: Database, last_event_id: int | None = None) -> None:
        await self.send_json(
            websocket,
            {"type": "connected", "serverTime": utc_now(), "replayFromEventId": last_event_id or 0},
        )
        threads = db.list_threads()
        turns = [turn for thread in threads for turn in db.list_turns(thread.threadId)]
        approvals = db.list_pending_approvals()
        await self.send_json(
            websocket,
            {
                "type": "snapshot",
                "snapshot": {
                    "threads": [_as_dict(thread) for thread in threads],
                    "turns": [_as_dict(turn) for turn in turns],
                    "pendingApprovals": [_as_dict(approval) for approval in approvals],
                },
            },
        )
        for event in db.list_events(after_event_id=last_event_id, limit=5000):
            await self.send_json(websocket, {"type": "replay.event", "event": _as_dict(event)})
        await self.send_json(websocket, {"type": "replay.complete", "lastEventId": db.last_event_id()})

    async def emit_event(self, event: EventRecord) -> None:
        await self.broadcast_json({"type": "event", "event": _as_dict(event)})

    async def emit_thread_created(self, thread: ThreadRecord) -> None:
        await self.broadcast_json({"type": "thread.created", "thread": _as_dict(thread)})

    async def emit_thread_forked(self, thread: ThreadRecord) -> None:
        await self.broadcast_json({"type": "thread.forked", "thread": _as_dict(thread)})

    async def emit_thread_updated(self, thread: ThreadRecord) -> None:
        await self.broadcast_json({"type": "thread.updated", "thread": _as_dict(thread)})

    async def emit_turn_updated(self, turn: TurnRecord) -> None:
        await self.broadcast_json({"type": "turn.updated", "turn": _as_dict(turn)})

    async def emit_approval_requested(self, approval: ApprovalRecord) -> None:
        await self.broadcast_json({"type": "approval.requested", "approval": _as_dict(approval)})

    async def emit_approval_responded(self, approval: ApprovalRecord) -> None:
        await self.broadcast_json({"type": "approval.responded", "approval": _as_dict(approval)})

    async def run_forever(self, websocket: WebSocket) -> None:
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            await self.disconnect(websocket)

