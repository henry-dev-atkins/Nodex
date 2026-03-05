from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import HTTPException

from .db import Database
from .models import ThreadRecord
from .ws import WebSocketHub


SpawnSessionFn = Callable[[], Awaitable[Any]]
RetireSessionFn = Callable[[Any], Awaitable[None]]
ThreadStartParamsFn = Callable[[], dict[str, Any]]
ThreadResumeParamsFn = Callable[[str, list[dict[str, Any]] | None], dict[str, Any]]
ThreadRecordFromCodexFn = Callable[[dict[str, Any], str | None], ThreadRecord]
SyncThreadSnapshotFn = Callable[[dict[str, Any], str | None, str | None, str | None], ThreadRecord]
UpdateLocalThreadFromCodexFn = Callable[[str, dict[str, Any]], ThreadRecord]
RemoteThreadIdFn = Callable[[ThreadRecord], str]
LineageTurnSnapshotsFn = Callable[[str, str | None, bool], list[dict[str, Any]]]
HistoryFromTurnSnapshotsFn = Callable[[list[dict[str, Any]], bool], list[dict[str, Any]]]
MonotonicTimeFn = Callable[[], float]


class LifecycleService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        sessions: MutableMapping[str, Any],
        session_lock: asyncio.Lock,
        spawn_session: SpawnSessionFn,
        retire_session: RetireSessionFn,
        thread_start_params: ThreadStartParamsFn,
        thread_resume_params: ThreadResumeParamsFn,
        thread_record_from_codex: ThreadRecordFromCodexFn,
        sync_thread_snapshot: SyncThreadSnapshotFn,
        update_local_thread_from_codex: UpdateLocalThreadFromCodexFn,
        remote_thread_id: RemoteThreadIdFn,
        lineage_turn_snapshots: LineageTurnSnapshotsFn,
        history_from_turn_snapshots: HistoryFromTurnSnapshotsFn,
        monotonic_time: MonotonicTimeFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._sessions = sessions
        self._session_lock = session_lock
        self._spawn_session = spawn_session
        self._retire_session = retire_session
        self._thread_start_params = thread_start_params
        self._thread_resume_params = thread_resume_params
        self._thread_record_from_codex = thread_record_from_codex
        self._sync_thread_snapshot = sync_thread_snapshot
        self._update_local_thread_from_codex = update_local_thread_from_codex
        self._remote_thread_id = remote_thread_id
        self._lineage_turn_snapshots = lineage_turn_snapshots
        self._history_from_turn_snapshots = history_from_turn_snapshots
        self._monotonic_time = monotonic_time

    async def start_thread(self, title: str | None = None) -> ThreadRecord:
        session = await self._spawn_session()
        result = await session.rpc.request_with_retry("thread/start", self._thread_start_params(), timeout_s=60)
        thread = result["thread"]
        session.local_thread_id = thread["id"]
        session.thread_id = thread["id"]
        session.last_used_monotonic = self._monotonic_time()
        async with self._session_lock:
            self._sessions[thread["id"]] = session
        thread_record = self._thread_record_from_codex(thread, title)
        self.db.upsert_thread(thread_record)
        await self.ws.emit_thread_created(thread_record)
        return thread_record

    async def get_or_resume_session(self, thread_id: str) -> Any:
        thread = self.db.get_thread(thread_id)
        if not thread:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        async with self._session_lock:
            session = self._sessions.get(thread_id)
        if session:
            if thread.parentThreadId and thread.status == "error":
                await self._retire_session(session)
                return await self.resume_child_session_from_db(thread)
            session.last_used_monotonic = self._monotonic_time()
            return session
        if thread.parentThreadId:
            return await self.resume_child_session_from_db(thread)
        session = await self._spawn_session()
        remote_thread_id = self._remote_thread_id(thread)
        result = await session.rpc.request_with_retry(
            "thread/resume",
            self._thread_resume_params(remote_thread_id, None),
            timeout_s=60,
        )
        resumed_thread = result["thread"]
        session.local_thread_id = thread_id
        session.thread_id = resumed_thread["id"]
        session.last_used_monotonic = self._monotonic_time()
        if thread_id == resumed_thread["id"]:
            self._sync_thread_snapshot(resumed_thread, None, None, None)
        else:
            self._update_local_thread_from_codex(thread_id, resumed_thread)
        async with self._session_lock:
            self._sessions[thread_id] = session
        return session

    async def resume_child_session_from_db(self, thread: ThreadRecord) -> Any:
        parent = self.db.get_thread(thread.parentThreadId or "")
        if not parent:
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "thread_unavailable", "message": "Missing parent thread for branch resume", "details": {}}},
            )
        history = self._history_from_turn_snapshots(
            self._lineage_turn_snapshots(thread.threadId, None, False),
            False,
        )
        session = await self._spawn_session()
        result = await session.rpc.request_with_retry(
            "thread/resume",
            self._thread_resume_params(self._remote_thread_id(parent), history),
            timeout_s=60,
        )
        session.local_thread_id = thread.threadId
        session.thread_id = result["thread"]["id"]
        session.last_used_monotonic = self._monotonic_time()
        self._update_local_thread_from_codex(thread.threadId, result["thread"])
        async with self._session_lock:
            self._sessions[thread.threadId] = session
        return session
