"""Runtime configuration.

The MCP server keeps its own app dir (job store, research artifacts, exports).
It is intentionally separate from the upstream adapter's login/profile storage
and from any other application's data directory.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir

DEFAULT_UPSTREAM_COMMAND = "notebooklm-mcp"


@dataclass(frozen=True)
class Config:
    app_dir: Path
    upstream_command: str = DEFAULT_UPSTREAM_COMMAND
    upstream_args: tuple[str, ...] = ()
    query_timeout: float = 240.0
    source_wait_timeout: float = 120.0
    import_timeout: float = 300.0
    task_lease_seconds: float = 120.0

    @property
    def db_path(self) -> Path:
        return self.app_dir / "notebook-dossier.sqlite3"

    @property
    def research_dir(self) -> Path:
        """Per-research artifacts produced by the provider (report, source text)."""
        return self.app_dir / "research"

    @property
    def export_root(self) -> Path:
        return self.app_dir / "export"

    def research_path(self, research_id: str) -> Path:
        return self.research_dir / research_id


def load_config() -> Config:
    app_dir_raw = os.environ.get("NLM_APP_DIR")
    app_dir = Path(app_dir_raw) if app_dir_raw else Path(user_data_dir("notebook-dossier", appauthor=False))
    command = os.environ.get("NLM_UPSTREAM_COMMAND", DEFAULT_UPSTREAM_COMMAND)
    args_raw = os.environ.get("NLM_UPSTREAM_ARGS", "")
    args = tuple(shlex.split(args_raw)) if args_raw.strip() else ()

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number, got {raw!r}") from exc

    return Config(
        app_dir=app_dir,
        upstream_command=command,
        upstream_args=args,
        query_timeout=_float("NLM_QUERY_TIMEOUT", 240.0),
        source_wait_timeout=_float("NLM_SOURCE_WAIT_TIMEOUT", 120.0),
        import_timeout=_float("NLM_IMPORT_TIMEOUT", 300.0),
        task_lease_seconds=_float("NLM_TASK_LEASE_SECONDS", 120.0),
    )
