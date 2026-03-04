from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import build_api_router
from .codex_manager import CodexManager
from .db import Database
from .security import build_token_dependency, load_or_create_session_token, verify_ws_token
from .settings import load_settings
from .util import APP_NAME, APP_VERSION
from .ws import WebSocketHub


def _no_cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code: int = 200):
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        response.headers.update(_no_cache_headers())
        return response


def _render_index(frontend_dir, token: str) -> str:
    html = (frontend_dir / "index.html").read_text(encoding="utf-8")
    return html.replace("__SESSION_TOKEN__", token).replace("__APP_VERSION__", APP_VERSION)


def create_app() -> FastAPI:
    settings = load_settings()
    token = load_or_create_session_token(settings.token_path)
    require_token = build_token_dependency(token)
    db = Database(settings.db_path)
    ws = WebSocketHub()
    manager = CodexManager(db=db, ws=ws, settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.verify_codex_installation()
        await manager.ensure_schema()
        housekeeper = asyncio.create_task(manager.housekeeping_loop())
        try:
            yield
        finally:
            housekeeper.cancel()
            await manager.close()
            db.close()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
    app.mount("/static", NoCacheStaticFiles(directory=settings.frontend_dir / "src"), name="static")
    app.include_router(build_api_router(db, manager, require_token))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        if hasattr(exc, "status_code") and hasattr(exc, "detail"):
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": {"code": "invalid_request", "message": str(exc.detail), "details": {}}}
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": str(exc), "details": {}}},
        )

    @app.get("/health")
    async def health() -> dict[str, str | bool]:
        return {"ok": True, "service": APP_NAME, "version": APP_VERSION}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_render_index(settings.frontend_dir, token), headers=_no_cache_headers())

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(settings.frontend_dir / "src" / "favicon.svg", headers=_no_cache_headers())

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        if not await verify_ws_token(websocket, token):
            return
        await ws.connect(websocket)
        raw_last_event_id = websocket.query_params.get("lastEventId")
        last_event_id = int(raw_last_event_id) if raw_last_event_id and raw_last_event_id.isdigit() else None
        await ws.send_initial_snapshot(websocket, db, last_event_id=last_event_id)
        await ws.run_forever(websocket)

    return app
