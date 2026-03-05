from __future__ import annotations

import asyncio
from unittest.mock import patch

from backend.app.session_runtime import start_initialized_rpc


class FakeRpc:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object], float]] = []
        self.notifications: list[str] = []

    async def request(self, method: str, params: dict[str, object], timeout_s: float = 60.0):
        self.requests.append((method, params, timeout_s))
        return {"ok": True}

    async def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        del params
        self.notifications.append(method)


async def _noop_msg_handler(_msg) -> None:
    return None


async def _noop_stderr_handler(_line: str) -> None:
    return None


async def _noop_exit_handler(_code: int | None) -> None:
    return None


def test_start_initialized_rpc_bootstraps_initialize_and_initialized_notification() -> None:
    fake_rpc = FakeRpc()
    captured_command: list[str] = []

    async def fake_start(
        command: list[str],
        notification_handler,
        server_request_handler,
        stderr_handler,
        exit_handler,
    ):
        del notification_handler, server_request_handler, stderr_handler, exit_handler
        captured_command.extend(command)
        return fake_rpc

    async def run_case() -> None:
        with patch("backend.app.session_runtime.CodexRpcClient.start", new=fake_start):
            rpc = await start_initialized_rpc(
                "codex",
                notification_handler=_noop_msg_handler,
                server_request_handler=_noop_msg_handler,
                stderr_handler=_noop_stderr_handler,
                exit_handler=_noop_exit_handler,
                app_name="codex-ui-wrapper-test",
                app_version="1.2.3",
            )
        assert rpc is fake_rpc

    asyncio.run(run_case())

    assert captured_command
    assert captured_command[-1] == "app-server"
    assert fake_rpc.requests == [
        (
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-ui-wrapper-test",
                    "title": "Codex UI Wrapper",
                    "version": "1.2.3",
                },
                "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
            },
            30,
        )
    ]
    assert fake_rpc.notifications == ["initialized"]
