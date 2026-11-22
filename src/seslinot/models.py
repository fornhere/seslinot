"""SesliNot katmanlar arası ortak Pydantic v2 veri sözleşmeleri."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class VoiceState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    TRANSCRIBING = "transcribing"


class NoteStatus(StrEnum):
    HAM = "ham"
    ISLENDI = "işlendi"
    ARASTIRILDI = "araştırıldı"
    ARSIV = "arşiv"


class NoteIntent(StrEnum):
    NOT = "not"
    FIKIR = "fikir"
    KARAR = "karar"
    GOREV = "görev"
    ARASTIRMA = "araştırma"
    GORUS = "görüş"


class JobStatus(StrEnum):
    PENDING_ROUTE = "PENDING_ROUTE"
    PENDING_CONFIRM = "PENDING_CONFIRM"
    QUEUED_OFFLINE = "QUEUED_OFFLINE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DISMISSED = "DISMISSED"
    TIMED_OUT = "TIMED_OUT"


class ResearchPhase(StrEnum):
    QUEUED = "queued"
    VAULT_SCAN = "vault_scan"
    DIRECTION = "direction"
    GATHERING = "gathering"
    VERIFYING = "verifying"
    SYNTHESIZING = "synthesizing"
    AUDITING = "auditing"
    WRITING = "writing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


PHASE_LABELS_TR: dict[str, str] = {
    ResearchPhase.QUEUED: "Kuyrukta bekliyor",
    ResearchPhase.VAULT_SCAN: "Hafıza kasası taranıyor",
    ResearchPhase.DIRECTION: "Yön tayini ve planlama",
    ResearchPhase.GATHERING: "Subagent'lar context topluyor",
    ResearchPhase.VERIFYING: "Kaynaklar ve alıntılar doğrulanıyor",
    ResearchPhase.SYNTHESIZING: "Türkçe rapor sentezleniyor",
    ResearchPhase.AUDITING: "Bağımsız atıf denetimi yapılıyor",
    ResearchPhase.WRITING: "Hafıza kasasına yazılıyor",
    ResearchPhase.COMPLETED: "Tamamlandı",
    ResearchPhase.FAILED: "Hata oluştu",
    ResearchPhase.CANCELLED: "İptal edildi",
}


# --- Audio & STT ---


class TranscriptChunk(BaseModel):
    index: int
    start_sec: float
    end_sec: float
    text: str
    rms_dbfs: float | None = None
    filtered_hallucination: bool = False


class TranscriptionResult(BaseModel):
    text: str
    chunks: list[TranscriptChunk] = Field(default_factory=list)
    duration_secs: float = 0.0
    language: str = "tr"
    stt_model: str = "whisper-large-v3-turbo"
    backend: str = "openai_http"
    audio_archive_path: str | None = None


# --- Voice Note ---


class VoiceNoteRecord(BaseModel):
    note_id: str
    title: str
    raw_transcript: str
    summary_bullets: list[str] = Field(default_factory=list)
    duration_secs: int = 0
    language: str = "tr"
    stt_model: str = "whisper-large-v3-turbo"
    scope: str = "user"
    topic: str = "genel"
    concepts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=lambda: ["sesli-not"])
    intent: NoteIntent = NoteIntent.NOT
    status: NoteStatus = NoteStatus.ISLENDI
    vault_rel_path: str = ""
    audio_path: str | None = None
    related_research: str = ""
    candidate_suggested: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


# --- Intent Router ---


class PrefilterResult(BaseModel):
    matched: bool
    reason: str
    matched_terms: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    is_research: bool
    confidence: float = Field(ge=0.0, le=1.0)
    intent_type: Literal[
        "imperative",
        "question_implied",
        "narrative_mention",
        "negated",
        "note_only",
    ]
    research_topic: str = ""
    research_questions: list[str] = Field(default_factory=list, max_length=5)
    depth: Literal["quick", "deep"] = "deep"
    reason: str = ""
    backend_used: str = "prefilter"


# --- Vault Context ---


class VaultContextHit(BaseModel):
    source_id: str  # V01, V02...
    path: str
    title: str = ""
    anchor: str = ""
    excerpt: str
    score: float = 0.0
    kind: Literal["catalog", "bilgi", "note", "concept"] = "note"


class VaultContextBundle(BaseModel):
    query: str
    scope: str = "user"
    matched_concepts: list[str] = Field(default_factory=list)
    matched_projects: list[str] = Field(default_factory=list)
    expanded_terms: list[str] = Field(default_factory=list)
    catalog_text: str = ""
    hits: list[VaultContextHit] = Field(default_factory=list)


# --- Multi-Agent Deep Research ---


class SubagentDirective(BaseModel):
    worker_id: str
    subtopic: str
    objective: str
    out_of_scope: str = ""
    queries_en: list[str] = Field(default_factory=list)
    queries_tr: list[str] = Field(default_factory=list)
    focus_source_types: list[str] = Field(default_factory=lambda: ["web"])


class ResearchPlan(BaseModel):
    title_tr: str
    topic_slug: str
    scope: str = "user"
    brief_summary: str
    key_questions: list[str] = Field(default_factory=list)
    directives: list[SubagentDirective] = Field(default_factory=list)
    freshness_months: int = 3
    concepts: list[str] = Field(default_factory=list)


class SubagentFinding(BaseModel):
    claim_id: str
    claim_tr: str
    exact_quote: str
    quote_language: Literal["en", "tr", "other"] = "en"
    url: str | None = None
    title: str = ""
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    source_type: Literal["web", "vault"] = "web"
    vault_path: str | None = None
    section_hint: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class SubagentOutput(BaseModel):
    worker_id: str
    subtopic: str
    status: Literal["completed", "partial", "failed"] = "completed"
    gaps_or_blockers: list[str] = Field(default_factory=list)
    findings: list[SubagentFinding] = Field(default_factory=list)


class SourceRecord(BaseModel):
    source_id: str  # S01..Sn veya V01..Vn
    source_type: Literal["web", "vault"] = "web"
    original_urls: list[str] = Field(default_factory=list)
    canonical_url: str = ""
    final_url: str = ""
    vault_path: str | None = None
    url_hash: str = ""
    title: str = ""
    published_at: str | None = None
    http_status: int | None = None
    fetch_status: Literal[
        "fetched_ok",
        "fetch_blocked_snippet_only",
        "dead_url",
        "vault_ok",
        "vault_missing",
    ] = "fetched_ok"
    seen_in_tool_trace: bool = False
    snapshot_path: str | None = None
    snapshot_sha256: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    referenced_by_workers: list[str] = Field(default_factory=list)
    is_primary: bool = False


class QuoteVerification(BaseModel):
    claim_id: str
    worker_id: str
    source_id: str
    quote_status: Literal[
        "exact_match",
        "near_match",
        "snippet_only",
        "ungrounded_quote",
        "dead_source",
    ]
    similarity: float = 0.0
    claim_tr: str
    exact_quote: str


class ClaimAuditItem(BaseModel):
    sentence: str
    source_ids: list[str] = Field(default_factory=list)
    verdict: Literal["SUPPORTED", "OVERSTATED", "CONTRADICTED", "UNSUPPORTED"]
    note: str = ""


class VerificationReport(BaseModel):
    total_sources: int = 0
    verified_sources: int = 0
    dead_sources: int = 0
    quote_exact_match: int = 0
    quote_near_match: int = 0
    quote_snippet_only: int = 0
    quote_ungrounded: int = 0
    ghost_citations: list[str] = Field(default_factory=list)
    leaked_urls: list[str] = Field(default_factory=list)
    audit_status: Literal["passed", "repaired", "failed_closed", "skipped"] = "passed"
    quote_checks: list[QuoteVerification] = Field(default_factory=list)
    audit_items: list[ClaimAuditItem] = Field(default_factory=list)


class ResearchReportInput(BaseModel):
    report_id: str
    title: str
    topic_slug: str
    scope: str = "user"
    topic: str = "genel"
    concepts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=lambda: ["araştırma", "derin-araştırma"])
    triggered_by_rel_path: str = ""
    previous_version_rel_path: str = ""
    freshness_months: int = 3
    body_markdown: str
    sources: list[SourceRecord] = Field(default_factory=list)
    verification: VerificationReport = Field(default_factory=VerificationReport)
    agent_count: int = 0
    duration_secs: int = 0
    confidence: Literal["yüksek", "orta", "düşük"] = "orta"
    vault_context_links: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


# --- DB / Queue / Desktop State ---


class ResearchJob(BaseModel):
    job_id: str
    note_id: str
    topic: str
    raw_prompt: str
    depth: Literal["quick", "deep"] = "deep"
    status: JobStatus = JobStatus.QUEUED
    phase: ResearchPhase = ResearchPhase.QUEUED
    phase_detail: str = ""
    confidence: float = 1.0
    unit_name: str = ""
    session_dir: str = ""
    report_rel_path: str = ""
    obsidian_uri: str = ""
    source_count: int = 0
    notif_id: int = 0
    attempt: int = 0
    max_attempts: int = 3
    next_retry_at: str | None = None
    error_message: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None


class ActiveSessionItem(BaseModel):
    id: str
    title: str
    phase: str
    phaseCode: str
    startedAt: int
    unit: str = ""


class PendingConfirmItem(BaseModel):
    id: str
    title: str
    confidence: float
    createdAt: int


class RecentReportItem(BaseModel):
    id: str
    title: str
    completedAt: int
    obsidianUri: str
    sourceCount: int = 0


class DesktopState(BaseModel):
    voiceState: str = VoiceState.IDLE.value
    recordingStartedAt: int = 0
    activeSessions: list[ActiveSessionItem] = Field(default_factory=list)
    pendingConfirm: list[PendingConfirmItem] = Field(default_factory=list)
    queuedOfflineCount: int = 0
    recentReports: list[RecentReportItem] = Field(default_factory=list)
