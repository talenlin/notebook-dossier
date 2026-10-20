"""Research flow: idempotent start, mode verification, findings sync, import accounting."""

from __future__ import annotations

import os

from conftest import call, start_research


async def test_start_is_idempotent_by_request_key(client, provider):
    first = await call(client, "dossier_start", topic="温度与寿命的关系", request_key="req-1")
    second = await call(client, "dossier_start", topic="温度与寿命的关系", request_key="req-1")
    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["idempotent_replay"] is True
    assert second["research_id"] == first["research_id"]
    assert len([c for c in provider.calls if c[0] == "research_start"]) == 1


async def test_mode_mismatch_is_not_silently_downgraded(client, provider):
    provider.start_response = {**provider.start_response, "mode": "fast"}
    out = await call(client, "dossier_start", topic="温度与寿命的关系")
    assert out["status"] == "error"
    assert "deep" in out["error"]
    status = await call(client, "dossier_status", research_id=out["research_id"])
    assert status["job_status"] == "error"
    assert any("mode_mismatch" in e for e in status["errors"])


async def test_status_syncs_full_findings_not_compact(client, provider):
    research_id = await start_research(client, provider)
    status_calls = [c for c in provider.calls if c[0] == "research_status"]
    assert status_calls[-1][1]["compact"] is False
    assert status_calls[-1][1]["max_wait"] == 0

    result = await call(client, "dossier_result", research_id=research_id, view="findings", include_report_text=True)
    assert result["total"] == 3
    assert result["report"]["truncated"] is False
    assert "结论" in result["report"]["text"]
    titles = [s["title"] for s in result["sources"]]
    assert titles == ["来源 A", "来源 B", "Deep Report"]
    report_row = result["sources"][2]
    assert report_row["is_report"] is True and report_row["importable"] is False
    # 跟踪参数在 URL 规范化时剥离
    assert result["sources"][1]["canonical_url"] == "https://b.example/y"


async def test_findings_paging(client, provider):
    research_id = await start_research(client, provider)
    page1 = await call(client, "dossier_result", research_id=research_id, cursor=0, limit=2)
    assert page1["total"] == 3 and len(page1["sources"]) == 2 and page1["next_cursor"] == 2
    page2 = await call(client, "dossier_result", research_id=research_id, cursor=2, limit=2)
    assert len(page2["sources"]) == 1 and page2["next_cursor"] is None


async def test_import_accounting_text_fetch_and_provenance_merge(client, provider, server):
    research_id = await start_research(client, provider)
    # 两个来源的索引文本完全相同（同一内容被转载/重复导入）→ 应归并为同一 provenance_group
    provider.contents["nb-src-2"] = provider.contents["nb-src-1"]
    out = await call(
        client,
        "dossier_import",
        research_id=research_id,
        exclude_reasons={f"{research_id}.s002": "报告伪来源不作为独立证据"},
    )
    assert out["status"] == "success", out
    mapping = {m["source_id"]: m for m in out["mapping"]}
    assert set(mapping) == {f"{research_id}.s000", f"{research_id}.s001"}
    assert all(m["import_status"] == "imported" for m in mapping.values())
    assert mapping[f"{research_id}.s000"]["notebook_source_id"] == "nb-src-1"

    # 报告伪来源被显式排除并记录理由
    result = await call(client, "dossier_result", research_id=research_id, view="findings")
    rows = {s["source_id"]: s for s in result["sources"]}
    assert rows[f"{research_id}.s002"]["import_status"] == "excluded"
    assert "报告伪来源" in rows[f"{research_id}.s002"]["import_reason"]

    await server.app.drain_background()

    # 后台预取来源文本；两个来源内容相同 → 归并为同一 provenance_group，独立证据只计一次
    result = await call(client, "dossier_result", research_id=research_id, view="findings")
    rows = {s["source_id"]: s for s in result["sources"]}
    assert rows[f"{research_id}.s000"]["read_status"] == "readable"
    assert rows[f"{research_id}.s001"]["read_status"] == "readable"
    assert rows[f"{research_id}.s000"]["provenance_group"] == rows[f"{research_id}.s001"]["provenance_group"]

    source_get = await call(client, "dossier_source_get", research_id=research_id, source_id=f"{research_id}.s000")
    assert source_get["source"]["text_hash"]
    assert source_get["source"]["char_count"] > 0
    assert "温度" in source_get["excerpt"]


async def test_import_auto_reconcile_backfills_mapping(client, provider, server):
    research_id = await start_research(client, provider)
    provider.import_response = {
        "status": "success",
        "notebook_id": "nb-1",
        "imported_count": 1,
        "imported_sources": [{"url": "https://a.example/x", "title": "来源 A", "source_id": "nb-src-1"}],
        "message": "imported",
    }
    out = await call(
        client,
        "dossier_import",
        research_id=research_id,
        exclude_reasons={f"{research_id}.s002": "报告伪来源不作为独立证据"},
    )
    assert out["status"] == "success"
    mapping = {m["source_id"]: m for m in out["mapping"]}
    # P0：导入收尾自动对账——映射漏配当场回填，不再挂 import_uncertain
    assert out["auto_reconcile"]["attempted"] is True
    assert out["auto_reconcile"]["remapped"] == [f"{research_id}.s001"]
    assert mapping[f"{research_id}.s000"]["import_status"] == "imported"
    assert mapping[f"{research_id}.s001"]["import_status"] == "imported"
    assert mapping[f"{research_id}.s001"]["notebook_source_id"] == "nb-src-2"
    assert not any(c[0] == "research_import" for c in provider.calls[3:])  # 对账不重复导入

    result = await call(client, "dossier_result", research_id=research_id, view="findings")
    rows = {s["source_id"]: s for s in result["sources"]}
    assert rows[f"{research_id}.s002"]["import_status"] == "excluded"
    assert "报告伪来源" in rows[f"{research_id}.s002"]["import_reason"]


async def test_import_stays_uncertain_when_absent_from_notebook(client, provider, server):
    research_id = await start_research(client, provider)
    provider.import_response = {
        "status": "success",
        "notebook_id": "nb-1",
        "imported_count": 1,
        "imported_sources": [{"url": "https://a.example/x", "title": "来源 A", "source_id": "nb-src-1"}],
        "message": "imported",
    }
    provider.notebook_sources_response = {
        "status": "success",
        "notebook": {"id": "nb-1", "source_count": 1},
        "sources": [{"id": "nb-src-1", "title": "来源 A"}],  # 无 来源 B
    }
    out = await call(client, "dossier_import", research_id=research_id)
    assert out["status"] == "success"
    mapping = {m["source_id"]: m for m in out["mapping"]}
    assert mapping[f"{research_id}.s001"]["import_status"] == "import_uncertain"
    assert out["auto_reconcile"]["still_uncertain"] == [f"{research_id}.s001"]
    assert "无同名来源" in mapping[f"{research_id}.s001"]["import_reason"]


async def test_task_id_conflict_blocks_import_until_confirmed(client, provider):
    research_id = await start_research(client, provider)
    # 完成态但无报告 → 语义存疑，保留冲突守门（confirm 通道）
    provider.status_response = {
        **provider.status_response,
        "task_id": "task-OTHER",
        "report": "",
        "sources": [],
    }
    status = await call(client, "dossier_status", research_id=research_id)
    assert status["task_id_conflict"] is True
    out = await call(client, "dossier_import", research_id=research_id)
    assert out["status"] == "error"
    assert out["code"] == "task_id_conflict"
    assert out["retry_safe"] is False
    assert "暂停" in out["error"]
    assert out["observed_task_id"] == "task-OTHER"

    # 人工核对后放行：采用观察到的任务 ID
    confirmed = await call(
        client, "dossier_import", research_id=research_id, confirm_task_id="task-OTHER"
    )
    assert confirmed["status"] == "success", confirmed
    assert all(m["import_status"] == "imported" for m in confirmed["mapping"])


async def test_task_id_renormalized_when_semantics_match(client, provider):
    research_id = await start_research(client, provider)
    # 同 Notebook 完成态 + 有报告 → ID 只是表示形式变化：自动采纳，不再阻断
    provider.status_response = {**provider.status_response, "task_id": "task-UUID-TYPE"}
    status = await call(client, "dossier_status", research_id=research_id)
    assert status["task_id_conflict"] is False
    assert status["task_id_note"]["renormalized_to"] == "task-UUID-TYPE"
    out = await call(client, "dossier_import", research_id=research_id)
    assert out["status"] == "success", out


async def test_error_envelope_is_structured(client, provider):
    out = await call(client, "dossier_status", research_id="r_missing")
    assert out["code"] == "not_found"
    assert out["recovery_tool"] == "dossier_start"
    assert out["retry_safe"] is True
    assert out["uncertain"] is False

    start = await call(client, "dossier_start", topic="信封测试", request_key="env-1")
    bad_view = await call(client, "dossier_result", research_id=start["research_id"], view="bogus")
    assert bad_view["code"] == "invalid_args"


async def test_tool_calls_are_audited_with_actor(client, provider, server):
    await call(client, "dossier_start", topic="审计测试", request_key="audit-1")
    rows = server.app.store.list_audit()
    tools = [r.tool for r in rows]
    assert "dossier_start" in tools
    entry = next(r for r in rows if r.tool == "dossier_start")
    assert entry.actor.startswith(f"pid:{os.getpid()}:")
    assert entry.args_keys == ["request_key", "scope", "title", "topic"]
    assert entry.research_id is None  # start 尚无 research_id


async def test_research_status_reports_per_source_progress(client, provider, server):
    research_id = await start_research(client, provider)
    status = await call(client, "dossier_status", research_id=research_id)
    progress = status["progress"]
    assert {p["source_id"] for p in progress} == {
        f"{research_id}.s000",
        f"{research_id}.s001",
        f"{research_id}.s002",
    }
    row = next(p for p in progress if p["source_id"] == f"{research_id}.s000")
    assert row["import_status"] == "discovered"
    assert row["evidence_cards"] == 0


async def test_reconcile_notebook_source_count_falls_back_when_null(client, provider, server):
    research_id = await start_research(client, provider)
    provider.import_response = {
        **provider.import_response,
        "imported_count": 1,
        "imported_sources": [provider.import_response["imported_sources"][0]],
    }
    provider.notebook_sources_response = {
        "status": "success",
        "notebook": {"id": "nb-1", "source_count": None},
        "sources": [{"id": "nb-src-1", "title": "来源 A"}],
    }
    out = await call(client, "dossier_import", research_id=research_id)
    assert out["status"] == "success"
    auto = out["auto_reconcile"]
    assert auto["attempted"] is True
    # 上游 source_count 显式为 null 时回退 len(remote)，不得透传 None
    assert auto["notebook_source_count"] == 1
    assert len(auto["still_uncertain"]) == 1
