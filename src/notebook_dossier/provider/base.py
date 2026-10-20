"""Provider abstraction between the MCP tool layer and NotebookLM backends.

The concrete implementation for the personal NotebookLM web product wraps the
upstream `notebooklm-mcp` stdio server (pinned `notebooklm-mcp-cli`). Tool
return envelopes follow that server's contract: wrapped tools report
``{"status": "success", ...}`` or ``{"status": "error", "error": ..., "hint": ...}``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Provider call failed; ``hint`` tells the caller the next corrective step."""

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        code: str = "provider_error",
        uncertain: bool = False,
        retry_safe: bool = True,
    ):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code
        self.uncertain = uncertain
        self.retry_safe = retry_safe

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "error": self.message,
            "uncertain": self.uncertain,
            "retry_safe": self.retry_safe,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


@runtime_checkable
class ResearchProvider(Protocol):
    async def auth_probe(self, profile: str | None = None) -> dict[str, Any]: ...

    async def research_start(
        self,
        query: str,
        source: str = "web",
        mode: str = "deep",
        notebook_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]: ...

    async def research_status(
        self,
        notebook_id: str,
        task_id: str | None = None,
        query: str | None = None,
        compact: bool = False,
        max_wait: int = 0,
    ) -> dict[str, Any]: ...

    async def research_import(
        self,
        notebook_id: str,
        task_id: str,
        source_indices: list[int] | None = None,
        cited_only: bool = False,
    ) -> dict[str, Any]: ...

    async def source_get_content(
        self,
        source_id: str,
        wait: bool = True,
        wait_timeout: float = 120.0,
    ) -> dict[str, Any]: ...

    async def notebook_query(
        self,
        notebook_id: str,
        query: str,
        source_ids: list[str],
        conversation_id: str | None = None,
        new_conversation: bool = True,
    ) -> dict[str, Any]: ...
