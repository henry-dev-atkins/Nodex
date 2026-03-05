from __future__ import annotations

from typing import Any


class ThreadParamsService:
    def __init__(self, *, workspace_dir: str, approval_policy: str, service_name: str) -> None:
        self._workspace_dir = workspace_dir
        self._approval_policy = approval_policy
        self._service_name = service_name

    def thread_start_params(self, ephemeral: bool = False, persist_extended_history: bool = True) -> dict[str, Any]:
        return {
            "cwd": self._workspace_dir,
            "approvalPolicy": self._approval_policy,
            "ephemeral": ephemeral,
            "experimentalRawEvents": False,
            "persistExtendedHistory": persist_extended_history,
            "serviceName": self._service_name,
        }

    def thread_resume_params(self, thread_id: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload = {
            "threadId": thread_id,
            "cwd": self._workspace_dir,
            "approvalPolicy": self._approval_policy,
            "persistExtendedHistory": True,
        }
        if history is not None:
            payload["history"] = history
        return payload
