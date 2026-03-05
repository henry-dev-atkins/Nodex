from __future__ import annotations

from backend.app.codex_manager import CodexSession, PendingTurn
from backend.app.session_policy import (
    is_idle_session,
    select_idle_sessions_for_eviction,
    select_session_for_capacity_retirement,
)


class FakeRpc:
    async def close(self) -> None:
        return None


def make_session(process_key: str, last_used: float) -> CodexSession:
    return CodexSession(process_key=process_key, rpc=FakeRpc(), last_used_monotonic=last_used)


def test_is_idle_session_requires_no_active_turn_or_pending_turn() -> None:
    idle = make_session("idle", 10.0)
    running = make_session("running", 20.0)
    running.active_turn_id = "turn-1"
    pending = make_session("pending", 30.0)
    pending.pending_turn = PendingTurn(idx=1, user_text="prompt")

    assert is_idle_session(idle) is True
    assert is_idle_session(running) is False
    assert is_idle_session(pending) is False


def test_select_idle_sessions_for_eviction_respects_cutoff_and_busy_state() -> None:
    old_idle = make_session("old-idle", 10.0)
    new_idle = make_session("new-idle", 100.0)
    old_busy = make_session("old-busy", 5.0)
    old_busy.active_turn_id = "turn-2"

    evictable = select_idle_sessions_for_eviction([old_idle, new_idle, old_busy], cutoff=50.0)

    assert evictable == [old_idle]


def test_select_session_for_capacity_retirement_returns_oldest_idle_when_at_limit() -> None:
    oldest = make_session("oldest", 10.0)
    newer = make_session("newer", 20.0)
    busy = make_session("busy", 1.0)
    busy.active_turn_id = "turn-3"

    reached, candidate = select_session_for_capacity_retirement([newer, busy, oldest], session_limit=3)

    assert reached is True
    assert candidate is oldest


def test_select_session_for_capacity_retirement_returns_busy_signal_when_no_idle_exists() -> None:
    busy_a = make_session("busy-a", 10.0)
    busy_a.active_turn_id = "turn-a"
    busy_b = make_session("busy-b", 20.0)
    busy_b.pending_turn = PendingTurn(idx=2, user_text="pending")

    reached, candidate = select_session_for_capacity_retirement([busy_a, busy_b], session_limit=2)

    assert reached is True
    assert candidate is None


def test_select_session_for_capacity_retirement_returns_not_reached_when_under_limit() -> None:
    one = make_session("one", 10.0)

    reached, candidate = select_session_for_capacity_retirement([one], session_limit=2)

    assert reached is False
    assert candidate is None
