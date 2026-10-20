"""Shared fixtures: FakeProvider (test-only) and in-memory MCP client."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastmcp import Client

from notebook_dossier.config import Config
from notebook_dossier.server import build_server

SOURCE_A_TEXT = (
    "温度是影响寿命的关键因素。实验显示：温度每升高 10 度，寿命减半。"
    "该结论来自 2019 年的一项小鼠实验，样本量为 20 只。"
)
SOURCE_B_TEXT = (
    "关于寿命影响因素，饮食控制的作用被多项研究提及。"
    "本文没有讨论温度与寿命的关系。"
)

EXTRACTION_ANSWER = {
    "source_view": {
        "main_thesis": "温度是寿命主因",
        "stance": "支持",
        "time_scope": "2019",
        "population_scope": "小鼠",
    },
    "claims": [
        {
            "claim": "温度升高会加速老化",
            "claim_type": "fact",
            "short_quote": "温度每升高 10 度，寿命减半",
            "locator": "第二段",
            "evidence_kind": "experiment",
            "conditions": "仅限小鼠实验",
            "limitations": "样本量 20 只",
        },
        {
            "claim": "杜撰主张：原文并不存在这句话",
            "claim_type": "fact",
            "short_quote": "这段文字从未在来源中出现过",
            "evidence_kind": "assertion",
        },
    ],
}


class FakeProvider:
    """Test-only provider double; records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.start_response: dict[str, Any] = {
            "status": "success",
            "task_id": "task-1",
            "notebook_id": "nb-1",
            "query": "",
            "source": "web",
            "mode": "deep",
            "message": "started",
        }
        self.status_response: dict[str, Any] = {
            "status": "completed",
            "notebook_id": "nb-1",
            "task_id": "task-1",
            "sources_found": 3,
            "sources": [
                {"url": "https://a.example/x", "title": "来源 A", "cited": True, "result_type": "web"},
                {"url": "https://b.example/y?utm_source=news", "title": "来源 B", "cited": False},
                {"url": "", "title": "Deep Report", "result_type": "deep_report", "cited": False},
            ],
            "report": "# 研究报告\n\n结论：温度与饮食均有影响。",
            "message": "Use research_import to add sources to notebook.",
        }
        self.import_response: dict[str, Any] = {
            "status": "success",
            "notebook_id": "nb-1",
            "imported_count": 2,
            "imported_sources": [
                {"url": "https://a.example/x", "title": "来源 A", "source_id": "nb-src-1"},
                {"url": "https://b.example/y", "title": "来源 B", "source_id": "nb-src-2"},
            ],
            "message": "imported",
        }
        self.contents: dict[str, str] = {"nb-src-1": SOURCE_A_TEXT, "nb-src-2": SOURCE_B_TEXT}
        self.notebook_sources_response: dict[str, Any] = {
            "status": "success",
            "notebook": {"id": "nb-1", "source_count": 2},
            "sources": [
                {"id": "nb-src-1", "title": "来源 A"},
                {"id": "nb-src-2", "title": "来源 B"},
            ],
        }
        self.answer_fn = None  # callable(dict) -> str | dict
        self.answer_queue: list[Any] = []
        self.query_log: list[dict[str, Any]] = []

    async def auth_probe(self, profile: str | None = None) -> dict[str, Any]:
        self.calls.append(("auth_probe", {"profile": profile}))
        return {"status": "success", "online_verified": True, "local": {"profiles": []}}

    async def research_start(
        self,
        query: str,
        source: str = "web",
        mode: str = "deep",
        notebook_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("research_start", {"query": query, "source": source, "mode": mode, "title": title}))
        return {**self.start_response, "query": query, "source": source, "mode": self.start_response.get("mode", mode)}

    async def research_status(
        self,
        notebook_id: str,
        task_id: str | None = None,
        query: str | None = None,
        compact: bool = False,
        max_wait: int = 0,
    ) -> dict[str, Any]:
        self.calls.append(
            ("research_status", {"notebook_id": notebook_id, "task_id": task_id, "compact": compact, "max_wait": max_wait})
        )
        return self.status_response

    async def research_import(
        self,
        notebook_id: str,
        task_id: str,
        source_indices: list[int] | None = None,
        cited_only: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("research_import", {"notebook_id": notebook_id, "task_id": task_id, "source_indices": source_indices}))
        return self.import_response

    async def source_get_content(
        self, source_id: str, wait: bool = True, wait_timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append(("source_get_content", {"source_id": source_id, "wait": wait}))
        content = self.contents.get(source_id, "")
        return {
            "status": "success",
            "content": content,
            "title": f"内容-{source_id}",
            "source_type": "web",
            "char_count": len(content),
        }

    async def notebook_sources(self, notebook_id: str) -> dict[str, Any]:
        self.calls.append(("notebook_sources", {"notebook_id": notebook_id}))
        return self.notebook_sources_response

    async def notebook_query(
        self,
        notebook_id: str,
        query: str,
        source_ids: list[str],
        conversation_id: str | None = None,
        new_conversation: bool = True,
    ) -> dict[str, Any]:
        self.query_log.append(
            {
                "notebook_id": notebook_id,
                "query": query,
                "source_ids": list(source_ids),
                "conversation_id": conversation_id,
                "new_conversation": new_conversation,
            }
        )
        self.calls.append(("notebook_query", {"source_ids": list(source_ids), "new_conversation": new_conversation}))
        if self.answer_fn is not None:
            answer = self.answer_fn(query)
        elif self.answer_queue:
            answer = self.answer_queue.pop(0)
        else:
            answer = "默认回答"
        if isinstance(answer, dict):
            return {"status": "success", **answer}
        return {
            "status": "success",
            "answer": answer,
            "question": query,
            "conversation_id": "conv-1",
            "sources_used": list(source_ids),
            "citations": {"1": source_ids[0]} if source_ids else {},
            "references": [{"source_id": source_ids[0], "citation_number": 1, "cited_text": "摘录"}] if source_ids else [],
        }


def json_answer(payload: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(app_dir=tmp_path / "app")


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def server(config, provider):
    return build_server(config, provider)


@pytest.fixture
async def client(server):
    async with Client(server) as c:
        yield c


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    texts = [getattr(b, "text", "") for b in (getattr(result, "content", None) or [])]
    return json.loads(texts[0]) if texts else {}


async def start_research(client: Client, provider: FakeProvider, request_key: str | None = None) -> str:
    out = await call(client, "dossier_start", topic="温度与寿命的关系", request_key=request_key)
    assert out["status"] == "success", out
    research_id = out["research_id"]
    status = await call(client, "dossier_status", research_id=research_id)
    assert status["upstream_state"] == "completed", status
    return research_id


def query_source_ids(prompt: str) -> list[str]:
    return re.findall(r'"source_id":\s*"([^"]+)"', prompt)
