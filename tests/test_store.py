"""Store-level persistence invariants."""

from __future__ import annotations

import pytest

from notebook_dossier.models import JobStatus, ResearchJob, TaskRow, TaskStatus, utcnow_iso
from notebook_dossier.store import Store


def test_request_key_is_unique(config):
    store = Store(config.db_path)
    store.create_job(ResearchJob(research_id="r1", request_key="k1", topic="t"))
    with pytest.raises(Exception):
        store.create_job(ResearchJob(research_id="r2", request_key="k1", topic="t"))
    assert store.get_job_by_request_key("k1").research_id == "r1"
    assert store.get_job_by_request_key("missing") is None


def test_question_upsert_is_idempotent(config):
    store = Store(config.db_path)
    q1 = store.upsert_question("r1", "同一个问题？")
    q2 = store.upsert_question("r1", "同一个问题？")
    assert q1.question_id == q2.question_id
    assert len(store.list_questions("r1")) == 1


def test_lease_expires_only_stale_tasks(config):
    store = Store(config.db_path)
    store.create_job(ResearchJob(research_id="r1", topic="t", status=JobStatus.researching))
    # 兄弟进程的活跃任务：心跳新鲜 → 不许打断
    store.create_task(
        TaskRow(
            task_id="r1.t001",
            research_id="r1",
            kind="evidence_build",
            owner_id="other:1",
            heartbeat_at=utcnow_iso(),
        )
    )
    # 已死进程的陈旧任务：心跳过期 → 接管为 interrupted
    store.create_task(
        TaskRow(
            task_id="r1.t002",
            research_id="r1",
            kind="evidence_build",
            owner_id="dead:2",
            heartbeat_at="2020-01-01T00:00:00+00:00",
        )
    )
    assert store.interrupt_stale_tasks(120.0) == 1
    assert store.get_task("r1.t001").status == TaskStatus.running
    stale = store.get_task("r1.t002")
    assert stale.status == TaskStatus.interrupted
    assert "lease expired" in (stale.error or "")


def test_touch_task_renews_lease(config):
    store = Store(config.db_path)
    store.create_job(ResearchJob(research_id="r1", topic="t"))
    store.create_task(
        TaskRow(
            task_id="r1.t001",
            research_id="r1",
            kind="evidence_build",
            heartbeat_at="2020-01-01T00:00:00+00:00",
        )
    )
    store.touch_task("r1.t001", result={"done": ["a|q"]})
    assert store.interrupt_stale_tasks(120.0) == 0
    task = store.get_task("r1.t001")
    assert task.status == TaskStatus.running
    assert task.result["done"] == ["a|q"]
    assert task.heartbeat_at > "2020-01-01"


def test_audit_log_roundtrip(config):
    from notebook_dossier.models import AuditEntry

    store = Store(config.db_path)
    store.add_audit(
        AuditEntry(actor="pid:1:ab", tool="dossier_start", research_id="r1", args_digest="abcd1234", args_keys=["topic"])
    )
    rows = store.list_audit("r1")
    assert rows[0].tool == "dossier_start"
    assert rows[0].actor == "pid:1:ab"
    assert store.list_audit("other") == []


def test_source_position_upsert_keeps_one_row_per_position(config):
    from notebook_dossier.models import SourceRow

    store = Store(config.db_path)
    first = SourceRow(source_id="r1.s000", research_id="r1", candidate_id="r1.s000", position=0, title="old")
    store.upsert_source(first)
    store.upsert_source(
        SourceRow(source_id="r1.s000", research_id="r1", candidate_id="r1.s000", position=0, title="new")
    )
    rows = store.list_sources("r1")
    assert len(rows) == 1
    assert rows[0].title == "new"
