"""Çekirdek yapılandırma, veri modelleri ve SQLite WAL kuyruğu testleri."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from seslinot.config import SesliNotConfig, load_config
from seslinot.db import SesliNotDB
from seslinot.models import (
    JobStatus,
    NoteIntent,
    ResearchJob,
    ResearchPhase,
    VoiceNoteRecord,
    VoiceState,
)


def test_config_loads_defaults_and_toml_override(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[vault]\npath = "/tmp/test-kasa"\nvault_name = "TestKasa"\n'
        '[audio]\nretention = "opus"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SESLINOT_VAULT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.vault.path == Path("/tmp/test-kasa")
    assert cfg.vault.vault_name == "TestKasa"
    assert cfg.audio.retention == "opus"
    assert cfg.research.lead_backend == "claude"
    assert cfg.research.worker_backend == "codex"


def test_db_note_and_atomic_job_claim(tmp_path: Path) -> None:
    db = SesliNotDB(tmp_path / "seslinot.db")
    note = VoiceNoteRecord(
        note_id="sn-20261122-1900-a1b2",
        title="PipeWire gecikme fikri",
        raw_transcript="PipeWire düşük gecikme ayarlarını araştır.",
        intent=NoteIntent.ARASTIRMA,
        vault_rel_path="sesli-notlar/2026-11/2026-11-22-1900-pipewire-gecikme.md",
    )
    db.insert_note(note)
    loaded = db.get_note(note.note_id)
    assert loaded is not None
    assert loaded.title == "PipeWire gecikme fikri"

    job1 = ResearchJob(
        job_id="res-001",
        note_id=note.note_id,
        topic="PipeWire düşük gecikme",
        raw_prompt=note.raw_transcript,
        status=JobStatus.QUEUED,
    )
    job2 = ResearchJob(
        job_id="res-002",
        note_id=note.note_id,
        topic="İkinci iş",
        raw_prompt="İkinci konu",
        status=JobStatus.QUEUED,
    )
    db.enqueue_job(job1)
    db.enqueue_job(job2)

    claimed1 = db.claim_next_job(max_concurrent=1)
    assert claimed1 is not None
    assert claimed1.job_id == "res-001"
    assert claimed1.status == JobStatus.RUNNING
    assert claimed1.attempt == 1

    # max_concurrent=1 dolduğu için ikinci iş alınmamalı
    assert db.claim_next_job(max_concurrent=1) is None

    db.update_job_phase("res-001", ResearchPhase.GATHERING, "3 alt ajan çalışıyor")
    events = db.list_events("res-001")
    assert len(events) == 1
    assert events[0]["phase"] == "gathering"

    state = db.build_desktop_state(VoiceState.RECORDING, recording_started_at=1700000000)
    assert state.voiceState == "recording"
    assert len(state.activeSessions) == 2  # 1 running + 1 queued


def test_offline_stale_job_protection(tmp_path: Path) -> None:
    db = SesliNotDB(tmp_path / "seslinot.db")
    note = VoiceNoteRecord(
        note_id="sn-1",
        title="Test",
        raw_transcript="Test araştır",
    )
    db.insert_note(note)

    fresh_job = ResearchJob(
        job_id="fresh",
        note_id="sn-1",
        topic="Taze iş",
        raw_prompt="Taze",
        status=JobStatus.QUEUED_OFFLINE,
    )
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    stale_job = ResearchJob(
        job_id="stale",
        note_id="sn-1",
        topic="Bayat iş",
        raw_prompt="Bayat",
        status=JobStatus.QUEUED_OFFLINE,
        created_at=stale_ts,
    )
    db.enqueue_job(fresh_job)
    db.enqueue_job(stale_job)

    promoted, staled = db.promote_offline_jobs(max_age_hours=24.0)
    assert (promoted, staled) == (1, 1)
    assert db.get_job("fresh").status == JobStatus.QUEUED
    assert db.get_job("stale").status == JobStatus.PENDING_CONFIRM


def test_paths_ensure_dirs(tmp_path: Path) -> None:
    cfg = SesliNotConfig()
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.paths.ensure_dirs()
    assert cfg.paths.sessions_dir.is_dir()
    assert cfg.paths.audio_dir.is_dir()
    assert cfg.paths.spool_dir.is_dir()
