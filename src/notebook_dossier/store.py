"""SQLite persistence for research jobs, sources, evidence, comparisons.

One connection guarded by a lock; the MCP server is single-process and all
writes happen on the event loop or its background tasks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import (
    AnswerRow,
    AuditEntry,
    ComparisonRow,
    EvidenceRow,
    Question,
    ResearchJob,
    SourceRow,
    SourceText,
    TaskRow,
    TaskStatus,
    utcnow_iso,
)

T = TypeVar("T", bound=BaseModel)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_jobs (
    research_id TEXT PRIMARY KEY,
    request_key TEXT UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    text TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (research_id, text)
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (research_id, position)
);
CREATE TABLE IF NOT EXISTS source_texts (
    source_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
    answer_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    research_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    tool TEXT NOT NULL,
    research_id TEXT,
    args_digest TEXT NOT NULL,
    args_keys TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_research ON sources (research_id);
CREATE INDEX IF NOT EXISTS idx_evidence_research ON evidence (research_id, question_id);
CREATE INDEX IF NOT EXISTS idx_comparisons_research ON comparisons (research_id, question_id);
CREATE INDEX IF NOT EXISTS idx_tasks_research ON tasks (research_id);
"""


def _dump(row: BaseModel) -> str:
    return row.model_dump_json()


def _load(model: type[T], payload: str) -> T:
    return model.model_validate_json(payload)


class Store:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- jobs

    def create_job(self, job: ResearchJob) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO research_jobs (research_id, request_key, payload) VALUES (?, ?, ?)",
                (job.research_id, job.request_key, _dump(job)),
            )
            self._conn.commit()

    def get_job(self, research_id: str) -> ResearchJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM research_jobs WHERE research_id = ?", (research_id,)
            ).fetchone()
        return _load(ResearchJob, row[0]) if row else None

    def get_job_by_request_key(self, request_key: str) -> ResearchJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM research_jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
        return _load(ResearchJob, row[0]) if row else None

    def update_job(self, research_id: str, **fields: Any) -> ResearchJob:
        job = self.get_job(research_id)
        if job is None:
            raise KeyError(f"unknown research_id: {research_id}")
        job = job.model_copy(update={**fields, "updated_at": utcnow_iso()})
        with self._lock:
            self._conn.execute(
                "UPDATE research_jobs SET payload = ? WHERE research_id = ?",
                (_dump(job), research_id),
            )
            self._conn.commit()
        return job

    # ------------------------------------------------------- questions

    def upsert_question(self, research_id: str, text: str) -> Question:
        text = text.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM questions WHERE research_id = ? AND text = ?",
                (research_id, text),
            ).fetchone()
            if row:
                return _load(Question, row[0])
            question = Question(question_id=f"q_{research_id}_{_short_id()}", research_id=research_id, text=text)
            self._conn.execute(
                "INSERT INTO questions (question_id, research_id, text, payload) VALUES (?, ?, ?, ?)",
                (question.question_id, research_id, text, _dump(question)),
            )
            self._conn.commit()
        return question

    def get_question(self, question_id: str) -> Question | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM questions WHERE question_id = ?", (question_id,)
            ).fetchone()
        return _load(Question, row[0]) if row else None

    def list_questions(self, research_id: str) -> list[Question]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM questions WHERE research_id = ? ORDER BY rowid", (research_id,)
            ).fetchall()
        return [_load(Question, r[0]) for r in rows]

    # --------------------------------------------------------- sources

    def upsert_source(self, source: SourceRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sources (source_id, research_id, candidate_id, position, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (research_id, position) DO UPDATE SET "
                "source_id = excluded.source_id, candidate_id = excluded.candidate_id, "
                "payload = excluded.payload",
                (source.source_id, source.research_id, source.candidate_id, source.position, _dump(source)),
            )
            self._conn.commit()

    def get_source(self, research_id: str, source_id: str) -> SourceRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM sources WHERE research_id = ? AND source_id = ?",
                (research_id, source_id),
            ).fetchone()
        return _load(SourceRow, row[0]) if row else None

    def resolve_source(self, research_id: str, ref: str) -> SourceRow | None:
        """Resolve by source_id or candidate_id."""
        found = self.get_source(research_id, ref)
        if found:
            return found
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM sources WHERE research_id = ? AND candidate_id = ?",
                (research_id, ref),
            ).fetchone()
        return _load(SourceRow, row[0]) if row else None

    def list_sources(self, research_id: str) -> list[SourceRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM sources WHERE research_id = ? ORDER BY position", (research_id,)
            ).fetchall()
        return [_load(SourceRow, r[0]) for r in rows]

    def update_source(self, research_id: str, source_id: str, **fields: Any) -> SourceRow:
        source = self.get_source(research_id, source_id)
        if source is None:
            raise KeyError(f"unknown source: {source_id}")
        source = source.model_copy(update={**fields, "updated_at": utcnow_iso()})
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET payload = ? WHERE research_id = ? AND source_id = ?",
                (_dump(source), research_id, source_id),
            )
            self._conn.commit()
        return source

    # ----------------------------------------------------- source texts

    def set_source_text(self, text: SourceText) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_texts (source_id, research_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT (source_id) DO UPDATE SET payload = excluded.payload",
                (text.source_id, text.research_id, _dump(text)),
            )
            self._conn.commit()

    def get_source_text(self, research_id: str, source_id: str) -> SourceText | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM source_texts WHERE research_id = ? AND source_id = ?",
                (research_id, source_id),
            ).fetchone()
        return _load(SourceText, row[0]) if row else None

    # --------------------------------------------------------- evidence

    def add_evidence(self, ev: EvidenceRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO evidence (evidence_id, research_id, source_id, question_id, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (ev.evidence_id, ev.research_id, ev.source_id, ev.question_id, _dump(ev)),
            )
            self._conn.commit()

    def list_evidence(
        self,
        research_id: str,
        question_id: str | None = None,
        source_id: str | None = None,
    ) -> list[EvidenceRow]:
        sql = "SELECT payload FROM evidence WHERE research_id = ?"
        params: list[Any] = [research_id]
        if question_id:
            sql += " AND question_id = ?"
            params.append(question_id)
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_load(EvidenceRow, r[0]) for r in rows]

    # ------------------------------------------------------ comparisons

    def add_comparison(self, c: ComparisonRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO comparisons (comparison_id, research_id, question_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (c.comparison_id, c.research_id, c.question_id, _dump(c)),
            )
            self._conn.commit()

    def delete_empty_comparisons(self, research_id: str, question_id: str) -> int:
        """Remove stub rows (no statements, no evidence) so retries replace them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT comparison_id, payload FROM comparisons WHERE research_id = ? AND question_id = ?",
                (research_id, question_id),
            ).fetchall()
            deleted = 0
            for comparison_id, payload in rows:
                row = _load(ComparisonRow, payload)
                if not row.statements and not row.evidence_ids:
                    self._conn.execute(
                        "DELETE FROM comparisons WHERE comparison_id = ?", (comparison_id,)
                    )
                    deleted += 1
            self._conn.commit()
        return deleted

    def list_comparisons(self, research_id: str, question_id: str | None = None) -> list[ComparisonRow]:
        sql = "SELECT payload FROM comparisons WHERE research_id = ?"
        params: list[Any] = [research_id]
        if question_id:
            sql += " AND question_id = ?"
            params.append(question_id)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_load(ComparisonRow, r[0]) for r in rows]

    # ---------------------------------------------------------- answers

    def add_answer(self, a: AnswerRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO answers (answer_id, research_id, payload) VALUES (?, ?, ?)",
                (a.answer_id, a.research_id, _dump(a)),
            )
            self._conn.commit()

    def list_answers(self, research_id: str) -> list[AnswerRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM answers WHERE research_id = ? ORDER BY rowid", (research_id,)
            ).fetchall()
        return [_load(AnswerRow, r[0]) for r in rows]

    def next_answer_seq(self, research_id: str) -> int:
        return len(self.list_answers(research_id)) + 1

    # ------------------------------------------------------------ tasks

    def create_task(self, task: TaskRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, research_id, kind, status, payload) VALUES (?, ?, ?, ?, ?)",
                (task.task_id, task.research_id, task.kind, task.status.value, _dump(task)),
            )
            self._conn.commit()

    def update_task(self, task_id: str, **fields: Any) -> TaskRow:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        task = task.model_copy(update=fields)
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, payload = ? WHERE task_id = ?",
                (task.status.value, _dump(task), task_id),
            )
            self._conn.commit()
        return task

    def get_task(self, task_id: str) -> TaskRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _load(TaskRow, row[0]) if row else None

    def list_tasks(self, research_id: str) -> list[TaskRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM tasks WHERE research_id = ? ORDER BY rowid", (research_id,)
            ).fetchall()
        return [_load(TaskRow, r[0]) for r in rows]

    def interrupt_stale_tasks(self, lease_seconds: float = 120.0) -> int:
        """Expire only tasks whose lease is stale (P0: 不误杀兄弟进程的活跃任务)。

        A running task is takeable only when its heartbeat is older than
        ``lease_seconds`` (or missing). Fresh-heartbeat tasks belong to a live
        runner elsewhere and are left untouched.
        """
        import datetime as _dt

        cutoff = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=lease_seconds)
        ).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, payload FROM tasks WHERE status = 'running'"
            ).fetchall()
            expired = 0
            for task_id, payload in rows:
                task = _load(TaskRow, payload)
                beat = task.heartbeat_at or task.created_at or ""
                if beat and beat >= cutoff:
                    continue  # 另一个进程仍在跑
                task = task.model_copy(
                    update={
                        "status": TaskStatus.interrupted,
                        "finished_at": utcnow_iso(),
                        "error": (
                            f"lease expired (owner={task.owner_id or 'unknown'}, "
                            f"last heartbeat={task.heartbeat_at or 'never'}); "
                            "重放对应工具即从断点恢复"
                        ),
                    }
                )
                self._conn.execute(
                    "UPDATE tasks SET status = ?, payload = ? WHERE task_id = ?",
                    (task.status.value, _dump(task), task_id),
                )
                expired += 1
            self._conn.commit()
        return expired

    def touch_task(self, task_id: str, result: dict[str, Any] | None = None) -> None:
        """Renew the lease (and optionally publish incremental progress)."""
        task = self.get_task(task_id)
        if task is None:
            return
        fields: dict[str, Any] = {"heartbeat_at": utcnow_iso()}
        if result is not None:
            fields["result"] = result
        task = task.model_copy(update=fields)
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, payload = ? WHERE task_id = ?",
                (task.status.value, _dump(task), task_id),
            )
            self._conn.commit()

    # ----------------------------------------------------------- audit log

    def add_audit(self, entry: AuditEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (ts, actor, tool, research_id, args_digest, args_keys) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.ts,
                    entry.actor,
                    entry.tool,
                    entry.research_id,
                    entry.args_digest,
                    json.dumps(entry.args_keys),
                ),
            )
            self._conn.commit()

    def list_audit(self, research_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        sql = "SELECT id, ts, actor, tool, research_id, args_digest, args_keys FROM audit_log"
        params: list[Any] = []
        if research_id:
            sql += " WHERE research_id = ?"
            params.append(research_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditEntry(
                id=r[0],
                ts=r[1],
                actor=r[2],
                tool=r[3],
                research_id=r[4],
                args_digest=r[5],
                args_keys=json.loads(r[6]),
            )
            for r in rows
        ]


def _short_id() -> str:
    return uuid.uuid4().hex[:10]
