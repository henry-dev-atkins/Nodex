from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .util import default_data_dir, ensure_directory, repo_root


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    codex_bin: str
    supported_codex_version_pattern: str
    data_dir: Path
    db_path: Path
    token_path: Path
    schema_cache_dir: Path
    frontend_dir: Path
    workspace_dir: Path
    approval_policy: str
    session_limit: int
    session_idle_ttl_s: int
    import_preview_ttl_s: int
    launch_browser: bool


def _resolve_data_dir(root: Path) -> Path:
    configured = os.environ.get("CODEX_UI_DATA_DIR")
    candidates = [Path(configured)] if configured else [default_data_dir(), root / ".codex_ui_data"]
    for candidate in candidates:
        try:
            return ensure_directory(candidate)
        except PermissionError:
            continue
    fallback = root / ".codex_ui_data"
    return ensure_directory(fallback)


def load_settings() -> Settings:
    root = repo_root()
    data_dir = _resolve_data_dir(root)
    return Settings(
        host=os.environ.get("CODEX_UI_HOST", "127.0.0.1"),
        port=int(os.environ.get("CODEX_UI_PORT", "8787")),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        supported_codex_version_pattern=os.environ.get("CODEX_UI_CODEX_VERSION_PATTERN", r"^0\.106\."),
        data_dir=data_dir,
        db_path=data_dir / "codex_ui_wrapper.db",
        token_path=data_dir / "session_token.txt",
        schema_cache_dir=ensure_directory(data_dir / "schema"),
        frontend_dir=Path(os.environ.get("CODEX_UI_FRONTEND_DIR", root / "frontend")),
        workspace_dir=Path(os.environ.get("CODEX_UI_WORKSPACE_DIR", root)),
        approval_policy=os.environ.get("CODEX_UI_APPROVAL_POLICY", "on-request"),
        session_limit=int(os.environ.get("CODEX_UI_SESSION_LIMIT", "4")),
        session_idle_ttl_s=int(os.environ.get("CODEX_UI_SESSION_IDLE_TTL_S", "600")),
        import_preview_ttl_s=int(os.environ.get("CODEX_UI_IMPORT_PREVIEW_TTL_S", "900")),
        launch_browser=os.environ.get("CODEX_UI_OPEN_BROWSER", "1").lower() not in {"0", "false", "no"},
    )
