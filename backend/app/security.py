from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Header, HTTPException, WebSocket


def load_or_create_session_token(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


def build_token_dependency(expected_token: str):
    def require_token(authorization: str | None = Header(default=None)) -> str:
        if authorization != f"Bearer {expected_token}":
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "code": "unauthorized",
                        "message": "Missing or invalid bearer token",
                        "details": {},
                    }
                },
            )
        return expected_token

    return require_token


async def verify_ws_token(websocket: WebSocket, expected_token: str) -> bool:
    if websocket.query_params.get("token") != expected_token:
        await websocket.close(code=4401)
        return False
    return True
