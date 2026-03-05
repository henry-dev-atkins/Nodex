from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .db import Database
from .models import TurnRecord
from .util import utc_now


NormalizeTurnStatusFn = Callable[[Any, str], str]
NowIsoFn = Callable[[], str]


class PendingTurnLike(Protocol):
    idx: int
    user_text: str


class TurnRecordService:
    def __init__(
        self,
        db: Database,
        *,
        normalize_turn_status: NormalizeTurnStatusFn,
        now_iso: NowIsoFn = utc_now,
    ) -> None:
        self.db = db
        self._normalize_turn_status = normalize_turn_status
        self._now_iso = now_iso

    def ensure_turn_record(self, thread_id: str, turn_id: str, status: str, pending: PendingTurnLike) -> TurnRecord:
        existing = self.db.get_turn(thread_id, turn_id)
        turn = TurnRecord(
            turnId=turn_id,
            threadId=thread_id,
            idx=existing.idx if existing else pending.idx,
            userText=existing.userText if existing else pending.user_text,
            status=self._normalize_turn_status(status, existing.status if existing else "running"),
            startedAt=existing.startedAt if existing else self._now_iso(),
            completedAt=existing.completedAt if existing else None,
            metadata=existing.metadata if existing else {},
        )
        self.db.upsert_turn(turn)
        return turn
