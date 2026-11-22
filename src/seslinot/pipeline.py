"""SesliNot uçtan uca orkestrasyon hattı (Ses → Not → Router → Araştırma → Kasa)."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from seslinot.audio import (
    STTClient,
    archive_or_cleanup_audio,
    play_cue,
)
from seslinot.config import SesliNotConfig
from seslinot.db import SesliNotDB
from seslinot.desktop.daemon import PhaseCallback, ResearchExecutionResult
from seslinot.desktop.notify import OmarchyNotifier, make_obsidian_uri
from seslinot.desktop.osd import OmarchyOSD
from seslinot.models import (
    JobStatus,
    NoteIntent,
    NoteStatus,
    ResearchJob,
    ResearchPhase,
    RouteDecision,
    VoiceNoteRecord,
    VoiceState,
    utc_now_iso,
)
from seslinot.research import ResearchOrchestrator, get_adapter
from seslinot.router import IntentRouter, RouterTransientError, stage1_prefilter
from seslinot.vault import HafizaBridge, VaultIndex, VaultWriter

logger = logging.getLogger(__name__)


def generate_note_id(now: datetime | None = None) -> str:
    dt = now or datetime.now().astimezone()
    return f"sn-{dt.strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"


def generate_job_id(now: datetime | None = None) -> str:
    dt = now or datetime.now().astimezone()
    return f"res-{dt.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def derive_title_from_transcript(text: str, max_chars: int = 72) -> str:
    clean = " ".join(text.strip().split())
    if not clean:
        return "Sesli not"
    first_sentence = clean.split(".")[0].split("?")[0].split("!")[0].strip()
    candidate = first_sentence if len(first_sentence) >= 8 else clean
    if len(candidate) <= max_chars:
        return candidate
    cut = candidate[:max_chars].rsplit(" ", 1)[0].strip()
    return (cut or candidate[:max_chars]).rstrip(",;:-") + "…"


def derive_summary_bullets(text: str) -> list[str]:
    clean = " ".join(text.strip().split())
    if not clean:
        return []
    parts = [p.strip() for p in clean.replace("!", ".").replace("?", ".").split(".") if p.strip()]
    if not parts:
        return [clean[:200]]
    return [p[:220] for p in parts[:3]]


class SesliNotPipeline:
    """Sesli not kaydı, yönlendirme ve çok-ajanlı araştırma akışlarını birleştirir."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        db: SesliNotDB | None = None,
        writer: VaultWriter | None = None,
        index: VaultIndex | None = None,
        bridge: HafizaBridge | None = None,
        router: IntentRouter | None = None,
        stt_client: STTClient | None = None,
        osd: OmarchyOSD | None = None,
        notifier: OmarchyNotifier | None = None,
        adapter_factory: Callable[[str], Any] = get_adapter,
        runner: Any | None = None,
        fetcher: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.cfg.paths.ensure_dirs()
        self.db = db or SesliNotDB(cfg.paths.db_path)
        self.writer = writer or VaultWriter(cfg)
        self.index = index or VaultIndex(cfg.vault.path, cfg.paths.fts_db_path)
        self.bridge = bridge or HafizaBridge(cfg, index=self.index)
        self.router = router or IntentRouter(cfg)
        self.stt_client = stt_client or STTClient(cfg)
        self.osd = osd or OmarchyOSD(cfg)
        self.notifier = notifier or OmarchyNotifier(cfg)
        self._adapter_factory = adapter_factory
        self._runner = runner
        self._fetcher = fetcher

    def _sync_state(
        self,
        voice_state: VoiceState = VoiceState.IDLE,
        recording_started_at: int = 0,
    ) -> None:
        state = self.db.build_desktop_state(
            voice_state=voice_state,
            recording_started_at=recording_started_at,
        )
        self.osd.write_desktop_state(state)

    async def ingest_transcript(
        self,
        raw_transcript: str,
        *,
        duration_secs: int = 0,
        language: str | None = None,
        stt_model: str = "whisper-large-v3-turbo",
        audio_path: str | None = None,
        force_research: bool = False,
        run_stage2_inline: bool = False,
        depth: str = "deep",
    ) -> dict[str, Any]:
        """Stage 0 (anında kasa yazımı) + Stage 1 (<1 ms ön-filtre) + isteğe bağlı Stage 2."""
        text = raw_transcript.strip()
        if not text:
            raise ValueError("Boş transkript kaydedilemez.")

        # 1) Kasa kavram/proje eşleştirme ve wikilink keşfi (salt-okunur, <50 ms)
        try:
            self.index.sync()
        except Exception as exc:
            logger.debug("FTS5 ön senkron atlandı: %s", exc)

        scope, concepts, _projects, _aliases = self.index.match_concepts_and_projects(text)
        wikilinks = self.index.discover_wikilinks(text, limit=5)

        pre = stage1_prefilter(text)
        should_route = force_research or pre.matched
        note_id = generate_note_id()
        title = derive_title_from_transcript(text)
        tags = ["sesli-not"]
        if should_route:
            tags.append("araştırma-tetiği")

        note = VoiceNoteRecord(
            note_id=note_id,
            title=title,
            raw_transcript=text,
            summary_bullets=derive_summary_bullets(text),
            duration_secs=max(0, int(duration_secs)),
            language=language or self.cfg.audio.language,
            stt_model=stt_model,
            scope=scope,
            topic=concepts[0] if concepts else "genel",
            concepts=concepts,
            tags=tags,
            intent=NoteIntent.ARASTIRMA if should_route else NoteIntent.NOT,
            status=NoteStatus.ISLENDI,
            audio_path=audio_path,
        )

        # 2) Stage 0: Ham notu hemen atomik + sır-taramalı kasaya yaz
        abs_path, rel_path, masked_rules = self.writer.write_voice_note(note, wikilinks=wikilinks)
        note.vault_rel_path = rel_path
        self.db.insert_note(note)

        try:
            self.index.sync()
        except Exception as exc:
            logger.debug("FTS5 sonrası senkron atlandı: %s", exc)

        obsidian_uri = make_obsidian_uri(self.cfg.vault.vault_name, rel_path)
        self.osd.show("Not kaydedildi", icon="󰄬", duration_ms=1200)
        self.notifier.notify_note_saved(note.title, obsidian_uri)

        job: ResearchJob | None = None
        route_decision: RouteDecision | None = None
        route_action: str = "note_only"

        # 3) Stage 1 -> Stage 2 yönlendirme
        if force_research:
            job = ResearchJob(
                job_id=generate_job_id(),
                note_id=note.note_id,
                topic=note.title,
                raw_prompt=text,
                depth="quick" if depth == "quick" else "deep",
                status=JobStatus.QUEUED,
                phase=ResearchPhase.QUEUED,
                confidence=1.0,
            )
            self.db.enqueue_job(job)
            route_action = "auto_start"
        elif pre.matched:
            if run_stage2_inline:
                try:
                    route_decision, route_action = await self.router.route(text)
                    if route_action == "auto_start":
                        job = ResearchJob(
                            job_id=generate_job_id(),
                            note_id=note.note_id,
                            topic=route_decision.research_topic or note.title,
                            raw_prompt=text,
                            depth=route_decision.depth,
                            status=JobStatus.QUEUED,
                            phase=ResearchPhase.QUEUED,
                            confidence=route_decision.confidence,
                        )
                        self.db.enqueue_job(job)
                    elif route_action == "confirm":
                        job = ResearchJob(
                            job_id=generate_job_id(),
                            note_id=note.note_id,
                            topic=route_decision.research_topic or note.title,
                            raw_prompt=text,
                            depth=route_decision.depth,
                            status=JobStatus.PENDING_CONFIRM,
                            phase=ResearchPhase.QUEUED,
                            confidence=route_decision.confidence,
                        )
                        self.db.enqueue_job(job)
                        self.notifier.notify_research_confirm(
                            job.job_id, job.topic, job.confidence
                        )
                except RouterTransientError:
                    job = ResearchJob(
                        job_id=generate_job_id(),
                        note_id=note.note_id,
                        topic=note.title,
                        raw_prompt=text,
                        depth="deep",
                        status=JobStatus.PENDING_ROUTE,
                        phase=ResearchPhase.QUEUED,
                        phase_detail="Yönlendirme bekleniyor",
                        confidence=0.0,
                    )
                    self.db.enqueue_job(job)
                    route_action = "pending_route"
            else:
                # Sıfır-gecikmeli varsayılan: Stage 2'yi seslinotd arka planda yapar
                job = ResearchJob(
                    job_id=generate_job_id(),
                    note_id=note.note_id,
                    topic=note.title,
                    raw_prompt=text,
                    depth="deep",
                    status=JobStatus.PENDING_ROUTE,
                    phase=ResearchPhase.QUEUED,
                    phase_detail="Araştırma niyeti doğrulanıyor",
                    confidence=0.0,
                )
                self.db.enqueue_job(job)
                route_action = "pending_route"

        self._sync_state(VoiceState.IDLE, 0)
        return {
            "note": note,
            "note_path": str(abs_path),
            "vault_rel_path": rel_path,
            "obsidian_uri": obsidian_uri,
            "masked_secrets": masked_rules,
            "prefilter": pre,
            "route_decision": route_decision,
            "route_action": route_action,
            "job": job,
        }

    async def ingest_wav(
        self,
        wav_path: Path,
        *,
        force_research: bool = False,
        run_stage2_inline: bool = False,
    ) -> dict[str, Any]:
        """Spool WAV dosyasını STT'den geçirir, notu yazar ve sesi arşivler/temizler."""
        self.osd.show("Yazıya dökülüyor...", icon="󰔟", duration_ms=0)
        self._sync_state(VoiceState.TRANSCRIBING, 0)
        try:
            stt_res = self.stt_client.transcribe_wav(wav_path)
            if not stt_res.text.strip():
                self.osd.show("Konuşma algılanmadı", icon="󰍭", duration_ms=1500)
                self._sync_state(VoiceState.IDLE, 0)
                return {"empty": True, "transcription": stt_res}

            result = await self.ingest_transcript(
                stt_res.text,
                duration_secs=int(round(stt_res.duration_secs)),
                language=stt_res.language,
                stt_model=stt_res.stt_model,
                force_research=force_research,
                run_stage2_inline=run_stage2_inline,
            )
            note: VoiceNoteRecord = result["note"]
            archived = archive_or_cleanup_audio(wav_path, note.note_id, self.cfg)
            result["audio_archive_path"] = str(archived) if archived else None
            result["transcription"] = stt_res
            play_cue("done", self.cfg, blocking=False)
            return result
        except Exception:
            self._sync_state(VoiceState.IDLE, 0)
            self.osd.close()
            raise

    async def route_job(self, job: ResearchJob) -> RouteDecision:
        """Daemon `PENDING_ROUTE` işleyici sözleşmesi."""
        decision, _action = await self.router.route(job.raw_prompt, force_stage2=True)
        return decision

    async def execute_research_job(
        self,
        job: ResearchJob,
        on_phase: PhaseCallback | None = None,
    ) -> ResearchExecutionResult:
        """Daemon ve CLI `--run-now` tarafından kullanılan uçtan uca araştırma yürütücüsü."""
        # Faz 0: Vault-First bağlam + önceki sürüm tespiti
        vault_context = self.bridge.gather_vault_context(job.topic, limit=6)
        note = self.db.get_note(job.note_id)
        triggered_by = note.vault_rel_path if note else ""

        prev_rel = ""
        try:
            prior_hits = self.index.search(
                job.topic, limit=2, folders=[self.cfg.vault.research_dir]
            )
            if prior_hits and prior_hits[0].score > 0:
                prev_rel = prior_hits[0].path
        except Exception:
            prev_rel = ""

        orchestrator = ResearchOrchestrator(
            self.cfg,
            adapter_factory=self._adapter_factory,
            runner=self._runner,
            fetcher=self._fetcher,
            progress_cb=on_phase,
        )
        session_dir = Path(job.session_dir) if job.session_dir else None
        report = await orchestrator.run(
            job,
            vault_context=vault_context,
            session_dir=session_dir,
            triggered_by_rel_path=triggered_by,
            previous_version_rel_path=prev_rel,
        )

        # Faz 4c: Kasa yazımı + wikilink + hash-korumalı geri bağlantılar
        if on_phase is not None:
            maybe = on_phase(ResearchPhase.WRITING, "Rapor Hafıza kasasına yazılıyor")
            if hasattr(maybe, "__await__"):
                await maybe

        wikilinks = self.index.discover_wikilinks(
            f"{report.title} {report.body_markdown[:1200]}", limit=5
        )
        _abs_path, report_rel_path, _masked = self.writer.write_research_report(
            report, wikilinks=wikilinks
        )

        if triggered_by:
            self.writer.link_research_to_voice_note(
                triggered_by, report_rel_path, report.title
            )
            self.db.update_note_research_link(
                job.note_id, f"[[{report_rel_path.removesuffix('.md')}]]"
            )

        if prev_rel and prev_rel != report_rel_path:
            self.writer.mark_previous_research_superseded(prev_rel, report_rel_path)

        try:
            self.index.sync()
        except Exception as exc:
            logger.debug("Rapor sonrası FTS5 senkron atlandı: %s", exc)

        obsidian_uri = make_obsidian_uri(self.cfg.vault.vault_name, report_rel_path)
        cited_count = len(report.sources)
        return ResearchExecutionResult(
            report_rel_path=report_rel_path,
            obsidian_uri=obsidian_uri,
            source_count=cited_count,
            session_dir=job.session_dir,
            unit_name=job.unit_name,
        )
