from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any

from .db import Database
from .session_policy import select_idle_sessions_for_eviction


class MaintenanceService:
    def __init__(
        self,
        db: Database,
        *,
        sessions: MutableMapping[str, Any],
        session_lock: asyncio.Lock,
        session_idle_ttl_s: int,
        session_close_timeout_s: float = 5.0,
    ) -> None:
        self.db = db
        self._sessions = sessions
        self._session_lock = session_lock
        self._session_idle_ttl_s = session_idle_ttl_s
        self._session_close_timeout_s = session_close_timeout_s

    async def housekeeping_step(self, now_iso: str, now_monotonic: float) -> None:
        self.db.delete_expired_import_previews(now_iso)
        await self.evict_idle_sessions(now_monotonic)

    async def close_sessions(self) -> None:
        async with self._session_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._close_rpc(session)

    async def evict_idle_sessions(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self._session_idle_ttl_s
        async with self._session_lock:
            idle_sessions = select_idle_sessions_for_eviction(list(self._sessions.values()), cutoff)
        for session in idle_sessions:
            await self.retire_session(session)

    async def retire_session(self, session: Any) -> None:
        session.intentional_close = True
        session_key = session.local_thread_id or session.thread_id
        if session_key:
            async with self._session_lock:
                current = self._sessions.get(session_key)
                if current is session:
                    self._sessions.pop(session_key, None)
        await self._close_rpc(session)

    async def _close_rpc(self, session: Any) -> None:
        try:
            await asyncio.wait_for(session.rpc.close(), timeout=self._session_close_timeout_s)
        except TimeoutError:
            process = getattr(session.rpc, "process", None)
            if process is None:
                return
            try:
                if getattr(process, "returncode", None) is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
            except Exception:
                return
