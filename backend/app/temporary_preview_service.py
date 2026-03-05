from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .session_runtime import start_initialized_rpc
from .util import APP_NAME, APP_VERSION


ThreadStartParamsFn = Callable[[], dict[str, Any]]
ApprovalResultFn = Callable[[str, str], dict[str, Any]]
ExtractMessageItemTextFn = Callable[[dict[str, Any]], str]
ExtractPreviewTextFn = Callable[[list[dict[str, Any]]], str]
RpcStarterFn = Callable[..., Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]


class TemporaryPreviewService:
    def __init__(
        self,
        *,
        codex_bin: str,
        approval_methods: set[str],
        thread_start_params: ThreadStartParamsFn,
        approval_result: ApprovalResultFn,
        extract_message_item_text: ExtractMessageItemTextFn,
        extract_preview_text_from_items: ExtractPreviewTextFn,
        rpc_starter: RpcStarterFn = start_initialized_rpc,
        app_name: str = APP_NAME,
        app_version: str = APP_VERSION,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._codex_bin = codex_bin
        self._approval_methods = approval_methods
        self._thread_start_params = thread_start_params
        self._approval_result = approval_result
        self._extract_message_item_text = extract_message_item_text
        self._extract_preview_text_from_items = extract_preview_text_from_items
        self._rpc_starter = rpc_starter
        self._app_name = app_name
        self._app_version = app_version
        self._sleep = sleep

    async def run_temporary_preview_prompt(self, prompt: str) -> str:
        completion_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        chunks: list[str] = []
        completed_messages: list[str] = []
        rpc_holder: dict[str, Any] = {}
        thread_id_holder: dict[str, str] = {}

        async def notification_handler(msg: dict[str, Any]) -> None:
            method = msg.get("method")
            params = msg.get("params", {})
            if method == "thread/started":
                thread = params.get("thread", {})
                if thread.get("id"):
                    thread_id_holder["thread_id"] = str(thread["id"])
                return
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if delta:
                    chunks.append(str(delta))
                return
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    text = self._extract_message_item_text(item)
                    if text:
                        completed_messages.append(text)
                return
            if method == "turn/completed" and not completion_future.done():
                result = completed_messages[-1] if completed_messages else "".join(chunks).strip()
                completion_future.set_result(result)
                return
            if method == "error" and not completion_future.done():
                error = params.get("error", {})
                completion_future.set_exception(RuntimeError(str(error.get("message") or "Preview generation failed")))

        async def server_request_handler(msg: dict[str, Any]) -> None:
            method = msg.get("method")
            rpc = rpc_holder["rpc"]
            await rpc.send_response(
                msg.get("id"),
                result=self._approval_result(method, "deny") if method in self._approval_methods else None,
                error=None if method in self._approval_methods else {"code": -32601, "message": f"Unsupported server request: {method}"},
            )

        async def stderr_handler(_line: str) -> None:
            return None

        async def exit_handler(code: int | None) -> None:
            if not completion_future.done():
                completion_future.set_exception(RuntimeError(f"Preview Codex session exited early: {code}"))

        rpc = await self._rpc_starter(
            self._codex_bin,
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            stderr_handler=stderr_handler,
            exit_handler=exit_handler,
            app_name=self._app_name,
            app_version=self._app_version,
        )
        rpc_holder["rpc"] = rpc
        try:
            started = await rpc.request_with_retry(
                "thread/start",
                self._thread_start_params(),
                timeout_s=60,
            )
            thread = started.get("thread", {})
            thread_id = str(thread.get("id") or thread_id_holder.get("thread_id") or "")
            if not thread_id:
                raise RuntimeError("Preview Codex session did not start a thread")
            started_turn = await rpc.request_with_retry(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                },
                timeout_s=600,
            )
            turn_id = str(started_turn.get("turn", {}).get("id", ""))
            for _ in range(120):
                snapshot = await rpc.request_with_retry(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": True},
                    timeout_s=60,
                )
                turns = snapshot.get("thread", {}).get("turns", [])
                current_turn = next((turn for turn in turns if str(turn.get("id")) == turn_id), None)
                if current_turn and str(current_turn.get("status")) not in {"inProgress", "running"}:
                    result = self._extract_preview_text_from_items(current_turn.get("items", []))
                    if result:
                        return result.strip()
                    break
                if completion_future.done():
                    result = completion_future.result()
                    if result:
                        return result.strip()
                await self._sleep(1)
            result = completed_messages[-1] if completed_messages else "".join(chunks).strip()
            if result:
                return result.strip()
            raise RuntimeError("Preview Codex session did not produce assistant output")
        finally:
            await rpc.close()
