from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from backend.app.codex_manager import CodexSession
from backend.app.db import Database
from backend.app.event_stream_service import EventStreamService
from backend.app.models import EventRecord


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeRpc:
    async def close(self) -> None:
        return None


class FakeWs:
    def __init__(self) -> None:
        self.events: list[EventRecord] = []

    async def emit_event(self, event: EventRecord) -> None:
        self.events.append(event)


def test_handle_notification_no_thread_context_is_noop() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "event_stream.db")
    ws = FakeWs()
    side_effect_calls: list[tuple[str, dict[str, object]]] = []
    try:
        service = EventStreamService(
            db=db,
            ws=ws,  # type: ignore[arg-type]
            extract_thread_id=lambda _payload: None,
            extract_turn_id=lambda _payload: None,
            apply_notification_side_effects=lambda _session, method, params: asyncio.sleep(
                0, result=side_effect_calls.append((method, params))
            ),
            monotonic_time=lambda: 0.0,
        )
        session = CodexSession(process_key="s", rpc=FakeRpc(), local_thread_id=None, thread_id=None)

        asyncio.run(service.handle_notification(session, {"method": "turn/started", "params": {"turnId": "t1"}}))

        assert ws.events == []
        assert db.list_events() == []
        assert side_effect_calls == []
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_notification_persists_event_updates_seq_and_invokes_side_effects() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "event_stream.db")
    ws = FakeWs()
    side_effect_calls: list[tuple[str, dict[str, object]]] = []
    try:
        async def apply_side_effects(_session, method: str, params: dict[str, object]) -> None:
            side_effect_calls.append((method, params))

        service = EventStreamService(
            db=db,
            ws=ws,  # type: ignore[arg-type]
            extract_thread_id=lambda payload: payload.get("threadId"),  # type: ignore[return-value]
            extract_turn_id=lambda payload: payload.get("turnId"),  # type: ignore[return-value]
            apply_notification_side_effects=apply_side_effects,
            monotonic_time=lambda: 42.0,
        )
        session = CodexSession(process_key="s", rpc=FakeRpc(), local_thread_id=None, thread_id="fallback-thread")

        asyncio.run(
            service.handle_notification(
                session,
                {"method": "turn/started", "params": {"threadId": "remote-thread", "turnId": "turn-1"}},
            )
        )

        assert session.thread_id == "remote-thread"
        assert session.event_seq_by_turn["turn-1"] == 1
        assert session.last_used_monotonic == 42.0
        assert len(ws.events) == 1
        assert ws.events[0].threadId == "remote-thread"
        assert ws.events[0].turnId == "turn-1"
        assert ws.events[0].seq == 1
        assert side_effect_calls == [("turn/started", {"threadId": "remote-thread", "turnId": "turn-1"})]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_handle_stderr_uses_thread_scope_sequence_and_emits_event() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "event_stream.db")
    ws = FakeWs()
    try:
        service = EventStreamService(
            db=db,
            ws=ws,  # type: ignore[arg-type]
            extract_thread_id=lambda _payload: None,
            extract_turn_id=lambda _payload: None,
            apply_notification_side_effects=lambda _session, _method, _params: asyncio.sleep(0),
            monotonic_time=lambda: 0.0,
        )
        session = CodexSession(process_key="s", rpc=FakeRpc(), local_thread_id="thread-1", thread_id="thread-1")

        asyncio.run(service.handle_stderr(session, "line 1"))
        asyncio.run(service.handle_stderr(session, "line 2"))

        assert session.event_seq_by_turn["__thread__"] == 2
        assert len(ws.events) == 2
        assert ws.events[0].seq == 1
        assert ws.events[1].seq == 2
        assert ws.events[0].payload == {"line": "line 1"}
        assert ws.events[1].payload == {"line": "line 2"}
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
