from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[None]]
StderrHandler = Callable[[str], Awaitable[None]]
ExitHandler = Callable[[int | None], Awaitable[None]]


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class CodexRpcClient:
    process: asyncio.subprocess.Process
    notification_handler: NotificationHandler
    server_request_handler: ServerRequestHandler
    stderr_handler: StderrHandler
    exit_handler: ExitHandler

    def __post_init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_counter = 0
        self._writer_lock = asyncio.Lock()
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._wait_task = asyncio.create_task(self._wait_loop())

    @classmethod
    async def start(
        cls,
        command: list[str],
        notification_handler: NotificationHandler,
        server_request_handler: ServerRequestHandler,
        stderr_handler: StderrHandler,
        exit_handler: ExitHandler,
    ) -> "CodexRpcClient":
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(
            process=process,
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            stderr_handler=stderr_handler,
            exit_handler=exit_handler,
        )

    async def _reader_loop(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await self.stderr_handler(f"Invalid JSON from Codex app-server: {raw}")
                continue
            if "method" in msg:
                if "id" in msg:
                    await self.server_request_handler(msg)
                else:
                    await self.notification_handler(msg)
                continue
            request_id = str(msg.get("id"))
            future = self._pending.pop(request_id, None)
            if future and not future.done():
                future.set_result(msg)

    async def _stderr_loop(self) -> None:
        assert self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            await self.stderr_handler(line.decode("utf-8", errors="replace").rstrip())

    async def _wait_loop(self) -> None:
        code = await self.process.wait()
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(JsonRpcError(-32099, "Codex process exited before responding", {"exitCode": code}))
        self._pending.clear()
        await self.exit_handler(code)

    async def _send_message(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        async with self._writer_lock:
            self.process.stdin.write((json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout_s: float = 60.0) -> Any:
        self._request_counter += 1
        request_id = f"client-{self._request_counter}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            err = response["error"]
            raise JsonRpcError(int(err.get("code", -32000)), err.get("message", "JSON-RPC error"), err.get("data"))
        return response.get("result")

    async def request_with_retry(self, method: str, params: dict[str, Any], timeout_s: float = 60.0) -> Any:
        backoff = [0.1, 0.2, 0.5, 1.0, 2.0]
        for delay in backoff:
            try:
                return await self.request(method, params, timeout_s=timeout_s)
            except JsonRpcError as exc:
                if exc.code != -32001:
                    raise
                await asyncio.sleep(delay)
        raise JsonRpcError(-32001, "Server overloaded; retries exhausted", None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send_message(payload)

    async def send_response(self, request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        await self._send_message(payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.process.terminate()
        await self.process.wait()
