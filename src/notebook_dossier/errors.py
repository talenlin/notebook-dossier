"""Structured error envelope (P1-7).

所有工具错误统一形状：
    {status: "error", code, error, hint?, uncertain, retry_safe, recovery_tool?, ...}

- ``code``: 机器可判的错误类别（调用方据此路由，不解析自然语言）
- ``uncertain``: 操作结果不确定（可能已在远端生效）——调用方必须先对账，禁止直接重放
- ``retry_safe``: 直接重试是否安全（有副作用操作失败时为 False）
- ``recovery_tool``: 建议的恢复入口工具名

``status``/``error``/``hint`` 保持旧键，兼容既有调用方。
"""

from __future__ import annotations

from typing import Any

# 机器可判的错误类别
NOT_FOUND = "not_found"
INVALID_ARGS = "invalid_args"
PRECONDITION_FAILED = "precondition_failed"
SOURCE_SCOPE = "source_scope"
TASK_ID_CONFLICT = "task_id_conflict"
MODE_MISMATCH = "mode_mismatch"
RESULT_UNCERTAIN = "result_uncertain"
PROVIDER_ERROR = "provider_error"
EXPORT_REFUSED = "export_refused"


def err(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    uncertain: bool = False,
    retry_safe: bool = True,
    recovery_tool: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "error",
        "code": code,
        "error": message,
        "uncertain": uncertain,
        "retry_safe": retry_safe,
    }
    if hint:
        out["hint"] = hint
    if recovery_tool:
        out["recovery_tool"] = recovery_tool
    out.update(extra)
    return out
