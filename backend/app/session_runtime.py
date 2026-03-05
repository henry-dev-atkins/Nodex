from __future__ import annotations

from .codex_rpc import CodexRpcClient, ExitHandler, NotificationHandler, ServerRequestHandler, StderrHandler
from .util import resolve_subprocess_command, split_command


async def start_initialized_rpc(
    codex_bin: str,
    notification_handler: NotificationHandler,
    server_request_handler: ServerRequestHandler,
    stderr_handler: StderrHandler,
    exit_handler: ExitHandler,
    *,
    app_name: str,
    app_version: str,
) -> CodexRpcClient:
    command = resolve_subprocess_command(split_command(codex_bin) + ["app-server"])
    rpc = await CodexRpcClient.start(
        command=command,
        notification_handler=notification_handler,
        server_request_handler=server_request_handler,
        stderr_handler=stderr_handler,
        exit_handler=exit_handler,
    )
    await rpc.request(
        "initialize",
        {
            "clientInfo": {"name": app_name, "title": "Codex UI Wrapper", "version": app_version},
            "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
        },
        timeout_s=30,
    )
    await rpc.notify("initialized")
    return rpc
