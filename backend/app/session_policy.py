from __future__ import annotations

from typing import Any


def is_idle_session(session: Any) -> bool:
    return not getattr(session, "active_turn_id", None) and not getattr(session, "pending_turn", None)


def select_idle_sessions_for_eviction(sessions: list[Any], cutoff: float) -> list[Any]:
    return [
        session
        for session in sessions
        if float(getattr(session, "last_used_monotonic", 0.0)) < cutoff and is_idle_session(session)
    ]


def select_session_for_capacity_retirement(sessions: list[Any], session_limit: int) -> tuple[bool, Any | None]:
    if len(sessions) < session_limit:
        return False, None
    idle_sessions = sorted(
        [session for session in sessions if is_idle_session(session)],
        key=lambda item: float(getattr(item, "last_used_monotonic", 0.0)),
    )
    if not idle_sessions:
        return True, None
    return True, idle_sessions[0]
