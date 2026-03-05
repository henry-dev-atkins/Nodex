from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import HTTPException

from .codex_rpc import JsonRpcError
from .db import Database
from .models import ThreadRecord
from .ws import WebSocketHub


GetOrResumeSessionFn = Callable[[str], Awaitable[Any]]
SpawnSessionFn = Callable[[], Awaitable[Any]]
RetireSessionFn = Callable[[Any], Awaitable[None]]
ThreadResumeParamsFn = Callable[[str, list[dict[str, Any]] | None], dict[str, Any]]
RemoteThreadIdFn = Callable[[ThreadRecord], str]
SyncThreadSnapshotFn = Callable[[dict[str, Any], str | None, str | None, str | None], ThreadRecord]
LineageTurnSnapshotsFn = Callable[[str, str | None, bool], list[dict[str, Any]]]
HistoryFromTurnSnapshotsFn = Callable[[list[dict[str, Any]], bool], list[dict[str, Any]]]
MonotonicTimeFn = Callable[[], float]


class BranchingService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        sessions: MutableMapping[str, Any],
        session_lock: asyncio.Lock,
        get_or_resume_session: GetOrResumeSessionFn,
        spawn_session: SpawnSessionFn,
        retire_session: RetireSessionFn,
        thread_resume_params: ThreadResumeParamsFn,
        remote_thread_id: RemoteThreadIdFn,
        sync_thread_snapshot: SyncThreadSnapshotFn,
        lineage_turn_snapshots: LineageTurnSnapshotsFn,
        history_from_turn_snapshots: HistoryFromTurnSnapshotsFn,
        monotonic_time: MonotonicTimeFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self._sessions = sessions
        self._session_lock = session_lock
        self._get_or_resume_session = get_or_resume_session
        self._spawn_session = spawn_session
        self._retire_session = retire_session
        self._thread_resume_params = thread_resume_params
        self._remote_thread_id = remote_thread_id
        self._sync_thread_snapshot = sync_thread_snapshot
        self._lineage_turn_snapshots = lineage_turn_snapshots
        self._history_from_turn_snapshots = history_from_turn_snapshots
        self._monotonic_time = monotonic_time

    async def fork_thread(self, thread_id: str, title: str | None = None) -> ThreadRecord:
        parent_session = await self._get_or_resume_session(thread_id)
        parent_turn_id = self.db.get_last_turn_id(thread_id)
        replayed_turn_count = len(self.db.list_turns(thread_id))
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
            self._thread_resume_params(child_thread_id, None),
            timeout_s=60,
        )
        resumed_thread = self._trim_replayed_turns(resumed_result["thread"], replayed_turn_count)
        resumed.local_thread_id = child_thread_id
        resumed.thread_id = child_thread_id
        resumed.last_used_monotonic = self._monotonic_time()
        async with self._session_lock:
            self._sessions[child_thread_id] = resumed
        thread_record = self._sync_thread_snapshot(
            resumed_thread,
            thread_id,
            parent_turn_id,
            title,
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
        lineage_turns = self._lineage_turn_snapshots(thread_id, turn_id, False)
        replayed_turn_count = len(lineage_turns)
        history = self._history_from_turn_snapshots(lineage_turns, False)
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
            resumed = await asyncio.wait_for(
                child_session.rpc.request_with_retry(
                    "thread/resume",
                    self._thread_resume_params(self._remote_thread_id(parent_thread), history),
                    timeout_s=60,
                ),
                timeout=65,
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
        child_session.last_used_monotonic = self._monotonic_time()
        if not child_thread.get("turns"):
            try:
                read_result = await asyncio.wait_for(
                    child_session.rpc.request_with_retry(
                        "thread/read",
                        {"threadId": child_thread_id, "includeTurns": True},
                        timeout_s=60,
                    ),
                    timeout=65,
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
        child_thread = self._trim_replayed_turns(child_thread, replayed_turn_count)
        async with self._session_lock:
            self._sessions[child_thread_id] = child_session
        thread_record = self._sync_thread_snapshot(
            child_thread,
            thread_id,
            turn_id,
            title,
        )
        await self.ws.emit_thread_forked(thread_record, turns=self.db.list_turns(child_thread_id))
        return thread_record

    def _trim_replayed_turns(self, codex_thread: dict[str, Any], replayed_turn_count: int) -> dict[str, Any]:
        turns = codex_thread.get("turns")
        if replayed_turn_count <= 0 or not isinstance(turns, list):
            return codex_thread
        start_index = min(replayed_turn_count, len(turns))
        return {**codex_thread, "turns": turns[start_index:]}
