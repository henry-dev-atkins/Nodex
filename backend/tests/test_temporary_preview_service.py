from __future__ import annotations

import asyncio
from typing import Any

from backend.app.temporary_preview_service import TemporaryPreviewService


class FakeRpc:
    def __init__(
        self,
        notification_handler,
        server_request_handler,
        *,
        mode: str,
    ) -> None:
        self._notification_handler = notification_handler
        self._server_request_handler = server_request_handler
        self._mode = mode
        self.thread_read_calls = 0
        self.responses: list[dict[str, Any]] = []
        self.closed = False

    async def request_with_retry(self, method: str, params: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        del timeout_s
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            self.thread_read_calls += 1
            if self._mode == "snapshot-completed":
                return {
                    "thread": {
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [{"type": "agentMessage", "text": "snapshot result"}],
                            }
                        ]
                    }
                }
            if self._mode == "completion-future":
                await self._notification_handler({"method": "item/agentMessage/delta", "params": {"delta": "delta result"}})
                await self._notification_handler({"method": "turn/completed", "params": {}})
                return {"thread": {"turns": [{"id": "turn-1", "status": "running", "items": []}]}}
            if self._mode == "approval-request":
                await self._server_request_handler({"id": "req-1", "method": "item/commandExecution/requestApproval"})
                return {
                    "thread": {
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [{"type": "agentMessage", "text": "approved path"}],
                            }
                        ]
                    }
                }
        raise AssertionError(f"Unexpected method: {method} params={params}")

    async def send_response(self, request_id: Any, result: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
        self.responses.append({"id": request_id, "result": result, "error": error})

    async def close(self) -> None:
        self.closed = True


def test_preview_returns_completed_snapshot_text() -> None:
    rpc_ref: dict[str, FakeRpc] = {}

    async def fake_starter(_codex_bin: str, **kwargs: Any) -> FakeRpc:
        rpc = FakeRpc(kwargs["notification_handler"], kwargs["server_request_handler"], mode="snapshot-completed")
        rpc_ref["rpc"] = rpc
        return rpc

    service = TemporaryPreviewService(
        codex_bin="codex",
        approval_methods={"item/commandExecution/requestApproval"},
        thread_start_params=lambda: {"ephemeral": True},
        approval_result=lambda _method, _decision: {"decision": "decline"},
        extract_message_item_text=lambda item: str(item.get("text", "")),
        extract_preview_text_from_items=lambda items: str(next((item.get("text") for item in items if item.get("type") == "agentMessage"), "")),
        rpc_starter=fake_starter,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    result = asyncio.run(service.run_temporary_preview_prompt("prompt"))

    assert result == "snapshot result"
    assert rpc_ref["rpc"].closed is True
    assert rpc_ref["rpc"].thread_read_calls == 1


def test_preview_uses_completion_future_when_turn_still_running() -> None:
    rpc_ref: dict[str, FakeRpc] = {}

    async def fake_starter(_codex_bin: str, **kwargs: Any) -> FakeRpc:
        rpc = FakeRpc(kwargs["notification_handler"], kwargs["server_request_handler"], mode="completion-future")
        rpc_ref["rpc"] = rpc
        return rpc

    service = TemporaryPreviewService(
        codex_bin="codex",
        approval_methods={"item/commandExecution/requestApproval"},
        thread_start_params=lambda: {"ephemeral": True},
        approval_result=lambda _method, _decision: {"decision": "decline"},
        extract_message_item_text=lambda item: str(item.get("text", "")),
        extract_preview_text_from_items=lambda items: str(next((item.get("text") for item in items if item.get("type") == "agentMessage"), "")),
        rpc_starter=fake_starter,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    result = asyncio.run(service.run_temporary_preview_prompt("prompt"))

    assert result == "delta result"
    assert rpc_ref["rpc"].closed is True


def test_preview_denies_approval_requests() -> None:
    rpc_ref: dict[str, FakeRpc] = {}

    async def fake_starter(_codex_bin: str, **kwargs: Any) -> FakeRpc:
        rpc = FakeRpc(kwargs["notification_handler"], kwargs["server_request_handler"], mode="approval-request")
        rpc_ref["rpc"] = rpc
        return rpc

    service = TemporaryPreviewService(
        codex_bin="codex",
        approval_methods={"item/commandExecution/requestApproval"},
        thread_start_params=lambda: {"ephemeral": True},
        approval_result=lambda _method, _decision: {"decision": "decline"},
        extract_message_item_text=lambda item: str(item.get("text", "")),
        extract_preview_text_from_items=lambda items: str(next((item.get("text") for item in items if item.get("type") == "agentMessage"), "")),
        rpc_starter=fake_starter,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    result = asyncio.run(service.run_temporary_preview_prompt("prompt"))

    assert result == "approved path"
    assert rpc_ref["rpc"].responses == [{"id": "req-1", "result": {"decision": "decline"}, "error": None}]
