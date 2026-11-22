"""Uçtan uca entegrasyon testleri: Ses → STT → Kasa → Router → Çok-Ajanlı Araştırma → Daemon → CLI."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from seslinot.cli import ctl_main, main as cli_main
from seslinot.config import SesliNotConfig
from seslinot.db import SesliNotDB
from seslinot.desktop import OmarchyNotifier, OmarchyOSD, SesliNotDaemon
from seslinot.models import (
    JobStatus,
    ResearchPhase,
    RouteDecision,
)
from seslinot.pipeline import SesliNotPipeline
from seslinot.research.adapters.base import WorkerRequest, WorkerResult
from seslinot.research.registry import FetchResponse


def _write_test_wav(path: Path, duration_secs: float = 2.0) -> Path:
    rate = 16000
    total = int(rate * duration_secs)
    frames = bytearray()
    for i in range(total):
        # 0.2s sessizlik + 1.4s 440Hz konuşma simülasyonu + 0.4s sessizlik
        t = i / rate
        amp = 9000 if 0.2 <= t <= 1.6 else 0
        sample = int(amp * math.sin(2.0 * math.pi * 440.0 * t))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
    return path


def _make_isolated_cfg(tmp_path: Path) -> SesliNotConfig:
    vault = tmp_path / "Hafıza"
    vault.mkdir()
    (vault / "beyin" / "kavramlar").mkdir(parents=True)
    (vault / "beyin" / "kavramlar" / "Linux ve sistem.md").write_text("# Linux ve sistem\n", encoding="utf-8")
    (vault / "komuta").mkdir()
    (vault / "komuta" / "kavramlar.json").write_text(
        json.dumps(
            {
                "kavramlar": [
                    {
                        "id": "linux",
                        "title": "Linux ve sistem",
                        "selectors": ["linux", "pipewire", "hyprland"],
                        "aliases": ["wayland", "ses"],
                        "related": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = SesliNotConfig()
    cfg.vault.path = vault
    cfg.vault.vault_name = "Hafıza"
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.audio.cues_enabled = False
    cfg.audio.duck_enabled = False
    cfg.audio.retention = "delete_after_verify"
    cfg.desktop.enable_osd = False
    cfg.desktop.enable_notifications = False
    cfg.desktop.enable_bar_refresh = False
    cfg.research.use_systemd_run = False
    cfg.paths.ensure_dirs()
    return cfg


class _FakeAdapter:
    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name

    def build_command(self, req: WorkerRequest):
        return (["fake", req.role], None, None)

    def parse_output(self, req: WorkerRequest, stdout_path: Path, output_file_path: Path | None):
        return WorkerResult(ok=True, exit_code=0, backend=self.backend_name)


class _FakeRunner:
    async def run(self, adapter: _FakeAdapter, req: WorkerRequest) -> WorkerResult:
        if req.role == "lead":
            payload = {
                "title_tr": "PipeWire Düşük Gecikme Mimarisi",
                "topic_slug": "pipewire-dusuk-gecikme",
                "scope": "user",
                "brief_summary": "PipeWire quantum ve sample rate ayarlarının incelenmesi.",
                "key_questions": ["Quantum değeri gecikmeyi nasıl etkiler?"],
                "freshness_months": 6,
                "concepts": ["linux"],
                "directives": [
                    {
                        "worker_id": "worker_01",
                        "subtopic": "PipeWire resmi quantum dokümantasyonu",
                        "objective": "PipeWire quantum ve buffer gecikmesini doğrula.",
                        "out_of_scope": "ALSA çekirdek sürücü geliştirme",
                        "queries_en": ["PipeWire clock.force-quantum latency docs"],
                        "queries_tr": ["PipeWire gecikme ayarları"],
                        "focus_source_types": ["web"],
                    },
                    {
                        "worker_id": "worker_02",
                        "subtopic": "Linux profesyonel ses kıyaslamaları",
                        "objective": "Gerçek zamanlı öncelik ve RTKit etkisini topla.",
                        "out_of_scope": "Windows WASAPI",
                        "queries_en": ["PipeWire RTKit real-time audio scheduling"],
                        "queries_tr": [],
                        "focus_source_types": ["web"],
                    },
                ],
            }
            return WorkerResult(ok=True, exit_code=0, parsed_json=payload, output_text=json.dumps(payload), backend=adapter.backend_name)

        if req.role == "subagent":
            url = (
                "https://docs.pipewire.org/page_man_pipewire_conf_5.html"
                if req.worker_id == "worker_01"
                else "https://gitlab.freedesktop.org/pipewire/pipewire/-/wikis/Performance-tuning"
            )
            quote = (
                "default.clock.quantum configures the default buffer size in samples."
                if req.worker_id == "worker_01"
                else "RTKit allows PipeWire threads to acquire real-time scheduling priority safely."
            )
            claim = (
                "default.clock.quantum ayarı PipeWire varsayılan tampon boyutunu örnek cinsinden belirler."
                if req.worker_id == "worker_01"
                else "RTKit, PipeWire iş parçacıklarının gerçek zamanlı öncelik almasını güvenli biçimde sağlar."
            )
            payload = {
                "worker_id": req.worker_id,
                "subtopic": req.worker_id,
                "status": "completed",
                "gaps_or_blockers": [],
                "findings": [
                    {
                        "claim_id": f"{req.worker_id}_c01",
                        "claim_tr": claim,
                        "exact_quote": quote,
                        "quote_language": "en",
                        "url": url,
                        "title": f"PipeWire Kaynağı ({req.worker_id})",
                        "published_at": "2026-10-01",
                        "source_type": "web",
                        "confidence": "high",
                    }
                ],
            }
            return WorkerResult(
                ok=True,
                exit_code=0,
                parsed_json=payload,
                output_text=json.dumps(payload),
                observed_urls=[url],
                backend=adapter.backend_name,
            )

        if req.role == "synthesizer":
            draft = (
                "## Yönetici Özeti\n\n"
                "- PipeWire varsayılan tampon boyutu `default.clock.quantum` ile belirlenir [^S01].\n"
                "- Gerçek zamanlı iş parçacığı önceliği için RTKit güvenli planlama sağlar [^S02].\n\n"
                "## Kasa ile Karşılaştırma ve Çelişkiler\n\n"
                "- Mevcut Linux notlarıyla çelişki bulunmadı.\n"
            )
            return WorkerResult(ok=True, exit_code=0, output_text=draft, backend=adapter.backend_name)

        if req.role == "auditor":
            audit_payload = {
                "items": [
                    {
                        "sentence": "PipeWire varsayılan tampon boyutu default.clock.quantum ile belirlenir.",
                        "source_ids": ["S01"],
                        "verdict": "SUPPORTED",
                        "note": "Tam destekleniyor.",
                    },
                    {
                        "sentence": "Gerçek zamanlı iş parçacığı önceliği için RTKit güvenli planlama sağlar.",
                        "source_ids": ["S02"],
                        "verdict": "SUPPORTED",
                        "note": "Tam destekleniyor.",
                    },
                ]
            }
            return WorkerResult(
                ok=True,
                exit_code=0,
                parsed_json=audit_payload,
                output_text=json.dumps(audit_payload),
                backend=adapter.backend_name,
            )
        raise AssertionError(f"Beklenmeyen rol: {req.role}")


async def _fake_fetcher(url: str) -> FetchResponse:
    if "pipewire_conf" in url:
        return FetchResponse(
            status_code=200,
            final_url=url,
            text="PipeWire configuration reference: default.clock.quantum configures the default buffer size in samples.",
            content_type="text/plain",
        )
    return FetchResponse(
        status_code=200,
        final_url=url,
        text="Performance guide: RTKit allows PipeWire threads to acquire real-time scheduling priority safely.",
        content_type="text/plain",
    )


class _StubSTT:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe_wav(self, wav_path: Path):
        from seslinot.models import TranscriptionResult

        return TranscriptionResult(
            text=self._text,
            duration_secs=2.0,
            language="tr",
            stt_model="whisper-large-v3-turbo",
            backend="mock_http",
        )


class _StubRouter:
    async def route(self, raw_transcript: str, *, force_stage2: bool = False):
        dec = RouteDecision(
            is_research=True,
            confidence=0.95,
            intent_type="imperative",
            research_topic="PipeWire düşük gecikme ayarları",
            research_questions=["default.clock.quantum nasıl ayarlanır?"],
            depth="deep",
            reason="Doğrudan araştırma emri",
            backend_used="mock",
        )
        return dec, "auto_start"


@pytest.mark.asyncio
async def test_full_end_to_end_voice_to_verified_research_in_vault(tmp_path: Path) -> None:
    """Ses WAV → STT → Sesli Not → Stage 1/2 Router → Daemon → 4+1 Fazlı Araştırma → Kasa + Geri Link."""
    cfg = _make_isolated_cfg(tmp_path)
    wav = _write_test_wav(tmp_path / "sample.wav")

    pipeline = SesliNotPipeline(
        cfg,
        stt_client=_StubSTT("PipeWire düşük gecikme ayarlarını derinlemesine araştır."),
        router=_StubRouter(),
        adapter_factory=lambda name: _FakeAdapter(name),
        runner=_FakeRunner(),
        fetcher=_fake_fetcher,
    )

    # 1. Sesli not kaydı + PENDING_ROUTE iş kuyruğuna ekleme
    ingest = await pipeline.ingest_wav(wav, run_stage2_inline=False)
    voice_rel = ingest["vault_rel_path"]
    voice_abs = cfg.vault.path / voice_rel
    assert voice_abs.is_file()
    assert not wav.exists()  # delete_after_verify temizledi
    assert ingest["job"] is not None
    assert ingest["job"].status == JobStatus.PENDING_ROUTE

    # 2. Daemon 1. tur: PENDING_ROUTE -> QUEUED + araştırma başlatılır
    daemon = SesliNotDaemon(
        cfg,
        db=pipeline.db,
        osd=pipeline.osd,
        notifier=pipeline.notifier,
        route_handler=pipeline.route_job,
        research_handler=pipeline.execute_research_job,
        network_checker=lambda: True,
    )
    tick = await daemon.run_once()
    assert tick.routed_jobs == 1
    assert tick.dispatched_jobs == 1
    await daemon.wait_for_active_tasks(timeout=15.0)

    # 3. Varsayılan onay kapısı: araştırma bitince kasa henüz kirlenmeden AWAITING_APPROVAL bekler
    job = pipeline.db.get_job(ingest["job"].job_id)
    assert job is not None
    assert job.status == JobStatus.AWAITING_APPROVAL
    assert job.phase == ResearchPhase.AWAITING_APPROVAL
    assert job.topic == "PipeWire düşük gecikme ayarları"
    assert job.source_count == 2
    assert job.report_rel_path == ""

    # Kasada araştırmalar altında henüz hiçbir rapor dosyası oluşturulmamış olmalı
    research_dir = cfg.vault.path / "araştırmalar"
    dated_reports_before = [
        p for p in research_dir.rglob("*.md")
        if p.name not in {"README.md", "Araştırma Haritası.md"}
    ]
    assert dated_reports_before == []

    # Sesli not da henüz 'araştırıldı' olarak güncellenmemiş olmalı
    assert "durum: araştırıldı" not in voice_abs.read_text(encoding="utf-8")

    # Önizleme ve staging dosyaları oturum dizininde hazır olmalı
    sess_dir = Path(job.session_dir)
    preview_file = sess_dir / "preview.md"
    assert preview_file.is_file()
    assert (sess_dir / "pending_report.json").is_file()
    assert "[ONAY BEKLİYOR]" in preview_file.read_text(encoding="utf-8")

    # state.json'da pendingApproval dolu, recentReports henüz boş olmalı
    state_before = json.loads(cfg.paths.state_json_path.read_text(encoding="utf-8"))
    assert len(state_before["pendingApproval"]) == 1
    assert state_before["pendingApproval"][0]["id"] == job.job_id
    assert state_before["pendingApproval"][0]["previewPath"] == str(preview_file)
    assert state_before["pendingApproval"][0]["sourceCount"] == 2
    assert state_before["recentReports"] == []

    # 4. Kullanıcı onaylar -> rapor kasaya atomik yazılır ve sesli not bağlanır
    approved = pipeline.approve_pending_report(job.job_id)
    assert approved is not None
    assert approved.report_rel_path.startswith("araştırmalar/")

    job = pipeline.db.get_job(ingest["job"].job_id)
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED
    assert job.phase == ResearchPhase.COMPLETED
    assert job.source_count == 2
    assert job.obsidian_uri.startswith("obsidian://open?vault=Haf%C4%B1za")

    report_abs = cfg.vault.path / job.report_rel_path
    assert report_abs.is_file()
    report_text = report_abs.read_text(encoding="utf-8")
    assert "## Kaynaklar" in report_text
    assert "https://docs.pipewire.org/page_man_pipewire_conf_5.html" in report_text
    assert "denetim_durumu: passed" in report_text

    # 5. Sesli not hash-güvenli biçimde araştırmaya bağlanmış olmalı
    updated_voice = voice_abs.read_text(encoding="utf-8")
    assert "durum: araştırıldı" in updated_voice
    assert job.report_rel_path.removesuffix(".md") in updated_voice

    # 6. Desktop state.json pendingApproval'ı boşaltıp son raporu yansıtmalı
    state_json = json.loads(cfg.paths.state_json_path.read_text(encoding="utf-8"))
    assert state_json["pendingApproval"] == []
    assert len(state_json["recentReports"]) == 1
    assert state_json["recentReports"][0]["sourceCount"] == 2

    # 7. Haritalar ve .base dosyaları oluşturulmuş olmalı
    assert (cfg.vault.path / "sesli-notlar" / "Sesli Notlar.base").is_file()
    assert (cfg.vault.path / "araştırmalar" / "Araştırmalar.base").is_file()
    assert (cfg.vault.path / "araştırmalar" / "Araştırma Haritası.md").is_file()


@pytest.mark.asyncio
async def test_research_approval_gate_reject_leaves_vault_clean(tmp_path: Path) -> None:
    """Kullanıcı raporu reddettiğinde kasaya hiçbir rapor yazılmaz ve sesli not değişmez."""
    cfg = _make_isolated_cfg(tmp_path)
    wav = _write_test_wav(tmp_path / "reject_sample.wav")

    pipeline = SesliNotPipeline(
        cfg,
        stt_client=_StubSTT("PipeWire düşük gecikme ayarlarını derinlemesine araştır."),
        router=_StubRouter(),
        adapter_factory=lambda name: _FakeAdapter(name),
        runner=_FakeRunner(),
        fetcher=_fake_fetcher,
    )
    ingest = await pipeline.ingest_wav(wav, run_stage2_inline=False)
    voice_abs = cfg.vault.path / ingest["vault_rel_path"]
    voice_before = voice_abs.read_text(encoding="utf-8")

    daemon = SesliNotDaemon(
        cfg,
        db=pipeline.db,
        osd=pipeline.osd,
        notifier=pipeline.notifier,
        route_handler=pipeline.route_job,
        research_handler=pipeline.execute_research_job,
        network_checker=lambda: True,
    )
    await daemon.run_once()
    await daemon.wait_for_active_tasks(timeout=15.0)

    job_id = ingest["job"].job_id
    job = pipeline.db.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.AWAITING_APPROVAL

    # Kullanıcı reddeder
    assert pipeline.reject_pending_report(job_id) is True
    # İkinci kez reddetme False döner
    assert pipeline.reject_pending_report(job_id) is False
    # Reddedilmiş rapor onaylanamaz
    assert pipeline.approve_pending_report(job_id) is None

    final_job = pipeline.db.get_job(job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.REJECTED
    assert final_job.report_rel_path == ""

    # Kasada hiç araştırma raporu yazılmamış olmalı
    research_dir = cfg.vault.path / "araştırmalar"
    dated_reports = [
        p for p in research_dir.rglob("*.md")
        if p.name not in {"README.md", "Araştırma Haritası.md"}
    ]
    assert dated_reports == []

    # Sesli not dokunulmamış olmalı
    assert voice_abs.read_text(encoding="utf-8") == voice_before

    # state.json hem pendingApproval hem recentReports boş olmalı
    state_after = json.loads(cfg.paths.state_json_path.read_text(encoding="utf-8"))
    assert state_after["pendingApproval"] == []
    assert state_after["recentReports"] == []



def test_cli_note_status_doctor_and_ctl(tmp_path: Path, capsys) -> None:
    cfg = _make_isolated_cfg(tmp_path)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[vault]\npath = "{cfg.vault.path.as_posix()}"\nvault_name = "Hafıza"\n'
        f'[paths]\ndata_dir = "{cfg.paths.data_dir.as_posix()}"\nstate_dir = "{cfg.paths.state_dir.as_posix()}"\nruntime_dir = "{cfg.paths.runtime_dir.as_posix()}"\n'
        "[desktop]\nenable_osd = false\nenable_notifications = false\nenable_bar_refresh = false\n"
        "[audio]\ncues_enabled = false\nduck_enabled = false\n",
        encoding="utf-8",
    )

    # Normal not (0 LLM çağrısı, araştırma açılmaz)
    rc = cli_main(["--config", str(cfg_file), "note", "Yarın marketten kahve alınacak."])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Not kaydedildi: sesli-notlar/" in out

    # Araştırmalı not (--research) -> kuyruğa alınır, seslinot-ctl cancel ile iptal edilir
    rc = cli_main(["--config", str(cfg_file), "note", "--research", "Wayland renk yönetimi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Araştırma işi:" in out

    # Status JSON kontrolü
    rc = cli_main(["--config", str(cfg_file), "status", "--json"])
    assert rc == 0
    status_data = json.loads(capsys.readouterr().out)
    assert len(status_data["recent_notes"]) == 2
    job_id = status_data["recent_jobs"][0]["id"]

    # seslinot-ctl cancel
    rc = ctl_main(["--config", str(cfg_file), "cancel", job_id])
    assert rc == 0

    # doctor JSON
    rc = cli_main(["--config", str(cfg_file), "doctor", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["vault_exists"] is True
    assert doc["platform"] in {"linux", "macos", "windows"}


def test_cli_init_non_interactive_and_roundtrip(tmp_path: Path, capsys) -> None:
    from seslinot.config import load_config

    vault_dir = tmp_path / "MyObsidian"
    cfg_file = tmp_path / "cfg" / "config.toml"

    rc = cli_main(
        [
            "--config",
            str(cfg_file),
            "init",
            "--non-interactive",
            "--vault",
            str(vault_dir),
            "--stt",
            "groq",
            "--backend",
            "api",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Kurulum tamamlandı" in out
    assert cfg_file.is_file()

    # Üzerine yazma koruması (--force olmadan rc=1)
    rc_dup = cli_main(["--config", str(cfg_file), "init", "--non-interactive", "--vault", str(vault_dir)])
    assert rc_dup == 1

    # Üretilen config.toml hatasız yüklenmeli ve banka klasörleri hazır olmalı
    loaded = load_config(cfg_file)
    assert loaded.vault.path.resolve() == vault_dir.resolve()
    assert loaded.audio.stt_provider == "groq"
    assert loaded.research.lead_backend == "api"
    assert loaded.research.require_save_approval is True
    assert (vault_dir / "sesli-notlar" / "Sesli Notlar.base").is_file()
    assert (vault_dir / "araştırmalar" / "Araştırmalar.base").is_file()

