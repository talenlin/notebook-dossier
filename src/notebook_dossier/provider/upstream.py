"""Adapter over the upstream `notebooklm-mcp` stdio server (notebooklm-mcp-cli 0.15.5).

Design constraints (see 方案 §6/§7/§11):
- side-effecting calls (research_start / research_import / notebook_query) are
  never retried automatically: a retry could start a second research task or a
  second import. Failures surface as ``结果不确定`` style errors with a hint.
- read-only calls may retry once after the child process connection breaks.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from .base import ProviderError

_INSTALL_HINT = (
    "确认已安装并可执行上游命令 notebooklm-mcp（推荐 uv sync --extra upstream 或 "
    "uv tool install 'git+https://github.com/jacob-bd/gemini-notebook-mcp-cli@a277ce7b81196222f79724b21973e17fc610999d'），"
    "并先用 `nlm login` 完成独立账号登录（`nlm login --check` 可验证）；"
    "也可用 NLM_UPSTREAM_COMMAND / NLM_UPSTREAM_ARGS 指定命令。"
)


class UpstreamMcpProvider:
    def __init__(
        self,
        command: str = "notebooklm-mcp",
        args: tuple[str, ...] = (),
        query_timeout: float = 240.0,
        source_wait_timeout: float = 120.0,
        import_timeout: float = 300.0,
        profile: str | None = None,
    ):
        self._command = command
        self._args = list(args)
        self._query_timeout = query_timeout
        self._source_wait_timeout = source_wait_timeout
        self._import_timeout = import_timeout
        self._profile = profile or os.environ.get("NLM_PROFILE") or None
        self._client: Any = None

    # ------------------------------------------------------------- plumbing

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        env = dict(os.environ)
        if self._profile:
            env["NLM_PROFILE"] = self._profile
        transport = StdioTransport(command=self._command, args=self._args, env=env)
        client = Client(transport)
        await client.__aenter__()
        self._client = client
        return client

    async def _reset_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass

    async def aclose(self) -> None:
        await self._reset_client()

    async def _call_soft(self, tool: str, args: dict[str, Any], timeout: float) -> tuple[dict[str, Any] | None, str | None]:
        """Return (result, error). Never raises for transport or tool errors."""
        import asyncio

        try:
            client = await self._ensure_client()
            result = await asyncio.wait_for(client.call_tool(tool, args), timeout=timeout)
        except Exception as exc:  # transport, protocol, or timeout
            await self._reset_client()
            return None, f"{tool}: {exc}"
        data = _extract_payload(result)
        if data.get("status") == "error":
            return data, str(data.get("error") or data)
        return data, None

    async def _call(
        self,
        tool: str,
        args: dict[str, Any],
        timeout: float,
        retryable: bool,
    ) -> dict[str, Any]:
        attempts = 2 if retryable else 1
        last_error: str | None = None
        for attempt in range(attempts):
            data, error = await self._call_soft(tool, args, timeout)
            if error is None and data is not None:
                return data
            last_error = error
            if not retryable:
                break
        raise ProviderError(
            f"上游工具 {tool} 调用失败（未自动重试有副作用的操作）: {last_error}",
            hint=_INSTALL_HINT,
        )

    # -------------------------------------------------------------- methods

    async def auth_probe(self, profile: str | None = None) -> dict[str, Any]:
        """Probe local auth state (server_info) and online state (notebook_list).

        ``profile`` switches the upstream profile (via ``NLM_PROFILE``) by
        restarting the upstream subprocess; switching is process-wide.
        """
        switched = False
        if profile and profile != self._profile:
            await self._reset_client()
            self._profile = profile
            switched = True
        info, info_err = await self._call_soft("server_info", {}, timeout=60.0)
        online, online_err = await self._call_soft("notebook_list", {"max_results": 1}, timeout=90.0)
        auth_status = (info or {}).get("auth_status")
        # 未配置登录的 profile 会让上游静默回退到默认 profile 会话（实测），
        # 因此 notebook_list 成功不能证明所选 profile 已连接。
        configured = auth_status == "configured"
        connected = online_err is None and configured
        return {
            "status": "success",
            "profile": self._profile or "(上游保存的默认 profile)",
            "profile_switched": switched,
            "auth_status": auth_status,
            "upstream_version": (info or {}).get("version"),
            "local": {
                "probe": "server_info",
                "error": info_err,
                "registered_tool_count": ((info or {}).get("mcp_capabilities") or {}).get("registered_tool_count"),
            },
            "online_verified": connected,
            "online_error": online_err if online_err else (None if configured else f"auth_status={auth_status!r}"),
            "next_action": None
            if connected
            else (
                f"profile {self._profile or 'default'} 未配置登录；上游可能回退到其他账号会话。"
                "请运行 `nlm login --profile <名>` 登录该 profile，并用 `nlm login --check --profile <名>` 验证后再用。"
            ),
        }

    async def research_start(
        self,
        query: str,
        source: str = "web",
        mode: str = "deep",
        notebook_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"query": query, "source": source, "mode": mode}
        if notebook_id:
            args["notebook_id"] = notebook_id
        if title:
            args["title"] = title
        return await self._call("research_start", args, timeout=120.0, retryable=False)

    async def research_status(
        self,
        notebook_id: str,
        task_id: str | None = None,
        query: str | None = None,
        compact: bool = False,
        max_wait: int = 0,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "notebook_id": notebook_id,
            "compact": compact,
            "max_wait": max_wait,
        }
        if task_id:
            args["task_id"] = task_id
        if query:
            args["query"] = query
        return await self._call("research_status", args, timeout=120.0, retryable=True)

    async def research_import(
        self,
        notebook_id: str,
        task_id: str,
        source_indices: list[int] | None = None,
        cited_only: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "notebook_id": notebook_id,
            "task_id": task_id,
            "timeout": self._import_timeout,
            "cited_only": cited_only,
        }
        if source_indices is not None:
            args["source_indices"] = source_indices
        return await self._call("research_import", args, timeout=self._import_timeout + 60.0, retryable=False)

    async def source_get_content(
        self,
        source_id: str,
        wait: bool = True,
        wait_timeout: float | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "source_id": source_id,
            "wait": wait,
            "wait_timeout": wait_timeout if wait_timeout is not None else self._source_wait_timeout,
        }
        return await self._call(
            "source_get_content", args, timeout=self._source_wait_timeout + 60.0, retryable=True
        )

    async def notebook_query(
        self,
        notebook_id: str,
        query: str,
        source_ids: list[str],
        conversation_id: str | None = None,
        new_conversation: bool = True,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "notebook_id": notebook_id,
            "query": query,
            "source_ids": source_ids,
            "new_conversation": new_conversation and conversation_id is None,
        }
        if conversation_id:
            args["conversation_id"] = conversation_id
        return await self._call("notebook_query", args, timeout=self._query_timeout, retryable=False)

    async def notebook_sources(self, notebook_id: str) -> dict[str, Any]:
        """List the notebook's actual sources (id + title) for import reconciliation."""
        return await self._call("notebook_get", {"notebook_id": notebook_id}, timeout=90.0, retryable=True)


def _extract_payload(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", None)
    if isinstance(data, BaseModel):
        data = data.model_dump()
    if not isinstance(data, dict):
        data = None
    if data is None:
        texts = [
            getattr(block, "text", "")
            for block in (getattr(result, "content", None) or [])
            if getattr(block, "text", None)
        ]
        if not texts:
            data = {}
        else:
            try:
                parsed = json.loads(texts[0])
                data = parsed if isinstance(parsed, dict) else {"value": parsed}
            except (ValueError, TypeError):
                data = {"text": texts[0]}
    if getattr(result, "is_error", False) and data.get("status") != "error":
        data = {"status": "error", "error": str(data.get("error") or data)}
    return data
