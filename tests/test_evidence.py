"""Evidence layer: single-source isolation, quote verification, parse-error honesty."""

from __future__ import annotations

import asyncio
import json

from conftest import EXTRACTION_ANSWER, call, json_answer, start_research
from fastmcp import Client
from notebook_dossier.config import Config
from notebook_dossier.models import TaskStatus
from notebook_dossier.server import build_server


async def _imported_research(client, provider, server):
    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()
    return research_id


async def test_evidence_build_single_source_isolation_and_quote_check(client, provider, server):
    research_id = await _imported_research(client, provider, server)
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)

    out = await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000"],
        questions=["温度如何影响寿命？"],
    )
    assert out["status"] == "success" and out["planned_extractions"] == 1
    await server.app.drain_background()

    extraction_calls = provider.query_log[-1:]
    assert extraction_calls, "应发起逐来源提取请求"
    # 逐来源取证：只选中该来源，且使用新会话
    assert extraction_calls[0]["source_ids"] == ["nb-src-1"]
    assert extraction_calls[0]["new_conversation"] is True

    result = await call(client, "dossier_result", research_id=research_id, view="evidence")
    assert result["total"] == 2
    by_claim = {e["claim"]: e for e in result["items"]}
    matched = by_claim["温度升高会加速老化"]
    assert matched["verification_status"] == "quote_matched"
    assert matched["conditions"] == "仅限小鼠实验"
    fabricated = by_claim["杜撰主张：原文并不存在这句话"]
    assert fabricated["verification_status"] == "quote_not_found"

    findings = await call(client, "dossier_result", research_id=research_id, view="findings")
    row = next(s for s in findings["sources"] if s["source_id"] == f"{research_id}.s000")
    assert row["analyzed_status"] == "analyzed"

    # 来源整体立场被记录在来源行
    source_get = await call(client, "dossier_source_get", research_id=research_id, source_id=f"{research_id}.s000")
    assert source_get["source"]["title"]


async def test_evidence_build_resumes_without_duplicates(client, provider, server):
    research_id = await _imported_research(client, provider, server)
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)
    args = dict(
        research_id=research_id,
        source_ids=[f"{research_id}.s000"],
        questions=["温度如何影响寿命？"],
    )
    await call(client, "dossier_evidence", **args)
    await server.app.drain_background()
    queries_after_first = len(provider.query_log)

    out2 = await call(client, "dossier_evidence", **args)
    assert out2["status"] == "success"
    await server.app.drain_background()

    # 断点续跑：已完成的 (来源, 问题) 组合不再提问、不产生重复卡
    assert len(provider.query_log) == queries_after_first
    result = await call(client, "dossier_result", research_id=research_id, view="evidence")
    assert result["total"] == 2
    tasks = await call(client, "dossier_result", research_id=research_id, view="tasks")
    ev_tasks = [t for t in tasks["items"] if t["kind"] == "evidence_build"]
    first, last = ev_tasks[0], ev_tasks[-1]
    assert last["result"]["skipped_pairs"] == 1
    assert last["result"]["done"] == []
    # P1 可观测性：任务结果按 (来源, 问题) 粒度自描述
    assert len(first["result"]["done"]) == 1
    assert first["result"]["done"][0].endswith("|" + first["params"]["question_ids"][0])


async def test_compare_truncation_is_disclosed(client, provider, server):
    research_id = await _imported_research(client, provider, server)
    claims = [
        {"claim": f"主张{i}", "claim_type": "fact", "short_quote": f"引文{i}", "evidence_kind": "data"}
        for i in range(15)
    ]

    def answer_fn(prompt: str) -> str:
        if "比较" in prompt:
            return json_answer({"statements": [], "relations": [], "unresolved_questions": []})
        return json_answer({"source_view": {}, "claims": claims})

    provider.answer_fn = answer_fn
    await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000"],
        questions=["Q？"],
    )
    await server.app.drain_background()
    out = await call(client, "dossier_compare", research_id=research_id, questions=["Q？"])
    assert out["status"] == "success"
    await server.app.drain_background()

    tasks = await call(client, "dossier_result", research_id=research_id, view="tasks")
    cmp_tasks = [t for t in tasks["items"] if t["kind"] == "viewpoints_compare"]
    assert any("截断" in n for n in cmp_tasks[-1]["result"]["truncated_notes"])
    comps = await call(client, "dossier_result", research_id=research_id, view="comparisons")
    assert any("截断" in n for n in comps["items"][0]["validation_notes"])


async def test_parse_error_is_marked_not_fake_success(client, provider, server):
    research_id = await _imported_research(client, provider, server)
    provider.answer_fn = lambda prompt: "这不是 JSON，只是自然语言回答。"

    await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000"],
        questions=["温度如何影响寿命？"],
    )
    await server.app.drain_background()

    result = await call(client, "dossier_result", research_id=research_id, view="evidence")
    assert result["total"] == 0
    findings = await call(client, "dossier_result", research_id=research_id, view="findings")
    row = next(s for s in findings["sources"] if s["source_id"] == f"{research_id}.s000")
    assert row["analyzed_status"] == "parse_error"


async def test_notebook_query_source_scope_is_enforced(client, provider, server):
    research_id = await _imported_research(client, provider, server)
    # 未导入的来源（报告伪来源）不能被选入提问
    out = await call(
        client,
        "dossier_query",
        research_id=research_id,
        question="温度影响是什么？",
        source_ids=[f"{research_id}.s002"],
    )
    assert out["status"] == "error"
    assert "未导入" in out["error"]

    # 显式单来源 + 新会话
    ok = await call(
        client,
        "dossier_query",
        research_id=research_id,
        question="温度影响是什么？",
        source_ids=[f"{research_id}.s000"],
    )
    assert ok["status"] == "success"
    assert ok["citations"][0]["citation_key"].endswith("#1")
    assert ok["citations"][0]["source_id"] == f"{research_id}.s000"
    assert provider.query_log[-1]["source_ids"] == ["nb-src-1"]
    assert provider.query_log[-1]["new_conversation"] is True

    # 外部来源 ID 一律拒绝
    foreign = await call(
        client,
        "dossier_query",
        research_id=research_id,
        question="温度影响是什么？",
        source_ids=["some-other-source"],
    )
    assert foreign["status"] == "error"


async def test_extraction_json_roundtrip_helpers():
    from notebook_dossier.evidence import extract_json_object, parse_extraction

    fenced = "前言\n```json\n{\"claims\": [{\"claim\": \"x\", \"short_quote\": \"y\"}]}\n```\n后记"
    obj = extract_json_object(fenced)
    assert obj is not None and obj["claims"][0]["claim"] == "x"
    draft, notes = parse_extraction(fenced)
    assert draft is not None and draft.claims[0].claim == "x"
    bad, bad_notes = parse_extraction("完全不是 JSON")
    assert bad is None and bad_notes


async def test_evidence_progress_lists_only_truly_skipped_pairs(client, provider, server):
    """回归：result.skipped 只列真正跳过的 (来源, 问题) 对，待提取的对不得混入。"""
    research_id = await _imported_research(client, provider, server)
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)
    src = f"{research_id}.s000"

    await call(client, "dossier_evidence", research_id=research_id, source_ids=[src], questions=["问题一？"])
    await server.app.drain_background()

    out = await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[src],
        questions=["问题一？", "问题二？"],
    )
    assert out["status"] == "success"
    await server.app.drain_background()

    tasks = await call(client, "dossier_result", research_id=research_id, view="tasks")
    last = [t for t in tasks["items"] if t["kind"] == "evidence_build"][-1]["result"]
    assert last["skipped_pairs"] == 1
    assert len(last["skipped"]) == 1 and last["skipped"][0].startswith(f"{src}|")
    assert len(last["done"]) == 1 and last["done"][0].startswith(f"{src}|")
    assert last["skipped"][0] != last["done"][0]


async def test_lease_renews_heartbeat_during_slow_extraction(tmp_path, provider):
    """回归：上游长调用期间心跳持续续租，兄弟进程不得把活跃任务误判为过期接管。"""
    config = Config(app_dir=tmp_path / "app", task_lease_seconds=0.5)
    server = build_server(config, provider)
    original = provider.notebook_query

    async def slow_query(*args, **kwargs):
        await asyncio.sleep(3.0)
        return await original(*args, **kwargs)

    provider.notebook_query = slow_query
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)
    async with Client(server) as client:
        research_id = await _imported_research(client, provider, server)
        out = await call(
            client,
            "dossier_evidence",
            research_id=research_id,
            source_ids=[f"{research_id}.s000"],
            questions=["温度如何影响寿命？"],
        )
        task_id = out["task_id"]
        # 提取进行中；秒级时间戳下检查点须晚于 租约+1s，才能区分有无 ticker
        await asyncio.sleep(2.0)
        expired = server.app.store.interrupt_stale_tasks(0.5)  # 兄弟进程尝试接管
        assert expired == 0
        row = server.app.store.get_task(task_id)
        assert row.status == TaskStatus.running
        await server.app.drain_background()
        row = server.app.store.get_task(task_id)
        assert row.status == TaskStatus.completed
