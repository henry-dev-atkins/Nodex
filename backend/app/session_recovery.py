from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .db import Database
from .models import ThreadRecord
from .ws import WebSocketHub


SpawnSessionFn = Callable[[], Awaitable[Any]]
ThreadResumeParamsFn = Callable[[str], dict[str, Any]]
SyncThreadSnapshotFn = Callable[[dict[str, Any]], ThreadRecord]
UpdateLocalThreadFromCodexFn = Callable[[str, dict[str, Any]], ThreadRecord]
MonotonicTimeFn = Callable[[], float]


class SessionRecoveryService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        sessions: MutableMapping[str, Any],
        session_lock: asyncio.Lock,
        spawn_session: SpawnSessionFn,
        thread_resume_params: ThreadResumeParamsFn,
        sync_thread_snapshot: SyncThreadSnapshotFn,
        update_local_thread_from_codex: UpdateLocalThreadFromCodexFn,
        monotonic_time: MonotonicTimeFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._sessions = sessions
        self._session_lock = session_lock
        self._spawn_session = spawn_session
        self._thread_resume_params = thread_resume_params
        self._sync_thread_snapshot = sync_thread_snapshot
        self._update_local_thread_from_codex = update_local_thread_from_codex
        self._monotonic_time = monotonic_time

    async def handle_exit(self, session: Any, code: int | None, *, stopping: bool) -> None:
        local_thread_id = session.local_thread_id or session.thread_id
        if stopping or not local_thread_id or not session.thread_id:
            return
        async with self._session_lock:
            current = self._sessions.get(local_thread_id)
            if current is session:
                self._sessions.pop(local_thread_id, None)
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
                resumed.last_used_monotonic = self._monotonic_time()
                if local_thread_id == resumed.thread_id:
                    self._sync_thread_snapshot(result["thread"])
                else:
                    self._update_local_thread_from_codex(local_thread_id, result["thread"])
                async with self._session_lock:
                    self._sessions[local_thread_id] = resumed
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
