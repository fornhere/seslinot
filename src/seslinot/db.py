"""SQLite WAL kalıcılık ve iş kuyruğu katmanı."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seslinot.models import (
    PHASE_LABELS_TR,
    ActiveSessionItem,
    DesktopState,
    JobStatus,
    PendingConfirmItem,
    RecentReportItem,
    ResearchJob,
    ResearchPhase,
    VoiceNoteRecord,
    VoiceState,
    utc_now_iso,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    raw_transcript TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '[]',
    duration_secs INTEGER NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT 'tr',
    stt_model TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'user',
    topic TEXT NOT NULL DEFAULT 'genel',
    concepts_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    intent TEXT NOT NULL DEFAULT 'not',
    status TEXT NOT NULL DEFAULT 'işlendi',
    vault_rel_path TEXT NOT NULL DEFAULT '',
    audio_path TEXT,
    related_research TEXT NOT NULL DEFAULT '',
    candidate_suggested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_jobs (
    job_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    raw_prompt TEXT NOT NULL,
    depth TEXT NOT NULL DEFAULT 'deep',
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    phase_detail TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    unit_name TEXT NOT NULL DEFAULT '',
    session_dir TEXT NOT NULL DEFAULT '',
    report_rel_path TEXT NOT NULL DEFAULT '',
    obsidian_uri TEXT NOT NULL DEFAULT '',
    source_count INTEGER NOT NULL DEFAULT 0,
    notif_id INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(note_id) REFERENCES notes(note_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON research_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id
    ON job_events(job_id, id);
"""


def _iso_to_epoch(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


class SesliNotDB:
    """Thread/process-güvenli SQLite WAL deposu."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    # --- Notes ---

    def insert_note(self, note: VoiceNoteRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (
                    note_id, title, raw_transcript, summary_json, duration_secs,
                    language, stt_model, scope, topic, concepts_json, tags_json,
                    intent, status, vault_rel_path, audio_path, related_research,
                    candidate_suggested, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.title,
                    note.raw_transcript,
                    json.dumps(note.summary_bullets, ensure_ascii=False),
                    note.duration_secs,
                    note.language,
                    note.stt_model,
                    note.scope,
                    note.topic,
                    json.dumps(note.concepts, ensure_ascii=False),
                    json.dumps(note.tags, ensure_ascii=False),
                    note.intent.value,
                    note.status.value,
                    note.vault_rel_path,
                    note.audio_path,
                    note.related_research,
                    int(note.candidate_suggested),
                    note.created_at,
                ),
            )

    def get_note(self, note_id: str) -> VoiceNoteRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE note_id = ?", (note_id,)
            ).fetchone()
        return self._row_to_note(row) if row else None

    def list_recent_notes(self, limit: int = 20) -> list[VoiceNoteRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_note(r) for r in rows]

    def update_note_research_link(
        self, note_id: str, related_research: str, status: str = "araştırıldı"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET related_research = ?, status = ? WHERE note_id = ?",
                (related_research, status, note_id),
            )

    # --- Research Jobs ---

    def enqueue_job(self, job: ResearchJob) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO research_jobs (
                    job_id, note_id, topic, raw_prompt, depth, status, phase,
                    phase_detail, confidence, unit_name, session_dir, report_rel_path,
                    obsidian_uri, source_count, notif_id, attempt, max_attempts,
                    next_retry_at, error_message, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.note_id,
                    job.topic,
                    job.raw_prompt,
                    job.depth,
                    job.status.value,
                    job.phase.value,
                    job.phase_detail,
                    job.confidence,
                    job.unit_name,
                    job.session_dir,
                    job.report_rel_path,
                    job.obsidian_uri,
                    job.source_count,
                    job.notif_id,
                    job.attempt,
                    job.max_attempts,
                    job.next_retry_at,
                    job.error_message,
                    job.created_at,
                    job.started_at,
                    job.finished_at,
                ),
            )

    def get_job(self, job_id: str) -> ResearchJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def claim_next_job(self, max_concurrent: int = 2) -> ResearchJob | None:
        """Atomik olarak sıradaki QUEUED işi RUNNING durumuna geçirir."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            active = conn.execute(
                "SELECT COUNT(*) FROM research_jobs WHERE status = ?",
                (JobStatus.RUNNING.value,),
            ).fetchone()[0]
            if active >= max_concurrent:
                conn.execute("ROLLBACK;")
                return None

            now = utc_now_iso()
            row = conn.execute(
                """
                UPDATE research_jobs
                SET status = ?, phase = ?, started_at = COALESCE(started_at, ?),
                    attempt = attempt + 1
                WHERE job_id = (
                    SELECT job_id FROM research_jobs
                    WHERE status = ?
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                )
                RETURNING *
                """,
                (
                    JobStatus.RUNNING.value,
                    ResearchPhase.VAULT_SCAN.value,
                    now,
                    JobStatus.QUEUED.value,
                    now,
                ),
            ).fetchone()
            conn.execute("COMMIT;")
            return self._row_to_job(row) if row else None
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        topic: str | None = None,
        depth: str | None = None,
        confidence: float | None = None,
        phase: ResearchPhase | None = None,
        phase_detail: str | None = None,
        error_message: str | None = None,
        report_rel_path: str | None = None,
        obsidian_uri: str | None = None,
        source_count: int | None = None,
        unit_name: str | None = None,
        session_dir: str | None = None,
        notif_id: int | None = None,
        next_retry_at: str | None = None,
        finished: bool = False,
    ) -> None:
        fields: list[str] = ["status = ?"]
        params: list[Any] = [status.value]
        if topic is not None:
            fields.append("topic = ?")
            params.append(topic)
        if depth is not None:
            fields.append("depth = ?")
            params.append(depth)
        if confidence is not None:
            fields.append("confidence = ?")
            params.append(confidence)
        if phase is not None:
            fields.append("phase = ?")
            params.append(phase.value)
        if phase_detail is not None:
            fields.append("phase_detail = ?")
            params.append(phase_detail)
        if error_message is not None:
            fields.append("error_message = ?")
            params.append(error_message)
        if report_rel_path is not None:
            fields.append("report_rel_path = ?")
            params.append(report_rel_path)
        if obsidian_uri is not None:
            fields.append("obsidian_uri = ?")
            params.append(obsidian_uri)
        if source_count is not None:
            fields.append("source_count = ?")
            params.append(source_count)
        if unit_name is not None:
            fields.append("unit_name = ?")
            params.append(unit_name)
        if session_dir is not None:
            fields.append("session_dir = ?")
            params.append(session_dir)
        if notif_id is not None:
            fields.append("notif_id = ?")
            params.append(notif_id)
        if next_retry_at is not None:
            fields.append("next_retry_at = ?")
            params.append(next_retry_at)
        if finished:
            fields.append("finished_at = ?")
            params.append(utc_now_iso())

        params.append(job_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE research_jobs SET {', '.join(fields)} WHERE job_id = ?",
                params,
            )

    def update_job_phase(
        self, job_id: str, phase: ResearchPhase, detail: str = ""
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE research_jobs SET phase = ?, phase_detail = ? WHERE job_id = ?",
                (phase.value, detail, job_id),
            )
        self.record_event(job_id, phase.value, detail or PHASE_LABELS_TR.get(phase, phase.value))

    def list_jobs(
        self, statuses: list[JobStatus] | None = None, limit: int = 50
    ) -> list[ResearchJob]:
        with self._connect() as conn:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = conn.execute(
                    f"SELECT * FROM research_jobs WHERE status IN ({placeholders}) "
                    "ORDER BY created_at DESC LIMIT ?",
                    [*[s.value for s in statuses], limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM research_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def list_offline_jobs(self) -> list[ResearchJob]:
        return self.list_jobs([JobStatus.PENDING_ROUTE, JobStatus.QUEUED_OFFLINE], limit=100)

    def promote_offline_jobs(self, max_age_hours: float = 24.0) -> tuple[int, int]:
        """Ağ döndüğünde taze offline işleri kuyruğa, bayatları onaya alır."""
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        promoted = 0
        staled = 0
        for job in self.list_jobs([JobStatus.QUEUED_OFFLINE], limit=100):
            age_hours = (now_epoch - _iso_to_epoch(job.created_at)) / 3600.0
            if age_hours <= max_age_hours:
                self.update_job_status(job.job_id, JobStatus.QUEUED, phase=ResearchPhase.QUEUED)
                promoted += 1
            else:
                self.update_job_status(job.job_id, JobStatus.PENDING_CONFIRM)
                staled += 1
        return promoted, staled

    # --- Events ---

    def record_event(
        self, job_id: str, phase: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO job_events (job_id, phase, message, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    job_id,
                    phase,
                    message,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )

    def list_events(self, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id ASC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "phase": r["phase"],
                "message": r["message"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # --- Desktop State Projection ---

    def build_desktop_state(
        self,
        voice_state: VoiceState = VoiceState.IDLE,
        recording_started_at: int = 0,
    ) -> DesktopState:
        running = self.list_jobs([JobStatus.RUNNING, JobStatus.QUEUED], limit=20)
        pending = self.list_jobs([JobStatus.PENDING_CONFIRM], limit=20)
        offline = self.list_jobs([JobStatus.PENDING_ROUTE, JobStatus.QUEUED_OFFLINE], limit=100)
        done = self.list_jobs([JobStatus.SUCCEEDED], limit=10)

        active_items = [
            ActiveSessionItem(
                id=j.job_id,
                title=j.topic,
                phase=j.phase_detail or PHASE_LABELS_TR.get(j.phase, j.phase.value),
                phaseCode=j.phase.value,
                startedAt=_iso_to_epoch(j.started_at or j.created_at),
                unit=j.unit_name,
            )
            for j in running
        ]
        pending_items = [
            PendingConfirmItem(
                id=j.job_id,
                title=j.topic,
                confidence=round(j.confidence, 2),
                createdAt=_iso_to_epoch(j.created_at),
            )
            for j in pending
        ]
        report_items = [
            RecentReportItem(
                id=j.job_id,
                title=j.topic,
                completedAt=_iso_to_epoch(j.finished_at or j.created_at),
                obsidianUri=j.obsidian_uri,
                sourceCount=j.source_count,
            )
            for j in done
            if j.obsidian_uri
        ]
        return DesktopState(
            voiceState=voice_state.value,
            recordingStartedAt=recording_started_at,
            activeSessions=active_items,
            pendingConfirm=pending_items,
            queuedOfflineCount=len(offline),
            recentReports=report_items,
        )

    # --- Row Mappers ---

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> VoiceNoteRecord:
        return VoiceNoteRecord(
            note_id=row["note_id"],
            title=row["title"],
            raw_transcript=row["raw_transcript"],
            summary_bullets=json.loads(row["summary_json"]),
            duration_secs=row["duration_secs"],
            language=row["language"],
            stt_model=row["stt_model"],
            scope=row["scope"],
            topic=row["topic"],
            concepts=json.loads(row["concepts_json"]),
            tags=json.loads(row["tags_json"]),
            intent=row["intent"],
            status=row["status"],
            vault_rel_path=row["vault_rel_path"],
            audio_path=row["audio_path"],
            related_research=row["related_research"],
            candidate_suggested=bool(row["candidate_suggested"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ResearchJob:
        return ResearchJob(
            job_id=row["job_id"],
            note_id=row["note_id"],
            topic=row["topic"],
            raw_prompt=row["raw_prompt"],
            depth=row["depth"],
            status=JobStatus(row["status"]),
            phase=ResearchPhase(row["phase"]),
            phase_detail=row["phase_detail"],
            confidence=row["confidence"],
            unit_name=row["unit_name"],
            session_dir=row["session_dir"],
            report_rel_path=row["report_rel_path"],
            obsidian_uri=row["obsidian_uri"],
            source_count=row["source_count"],
            notif_id=row["notif_id"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
