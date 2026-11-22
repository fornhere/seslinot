"""SesliNot komut satırı arayüzü (`seslinot` ve `seslinot-ctl`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Sequence

import httpx

from seslinot.audio import AudioCaptureController, CaptureError
from seslinot.config import SesliNotConfig, load_config
from seslinot.db import SesliNotDB
from seslinot.desktop import OmarchyNotifier, OmarchyOSD, SesliNotDaemon, make_obsidian_uri
from seslinot.models import (
    PHASE_LABELS_TR,
    JobStatus,
    ResearchPhase,
    VoiceState,
)
from seslinot.pipeline import SesliNotPipeline
from seslinot.vault import HafizaBridge


def _sync_capture_to_desktop(cfg: SesliNotConfig, db: SesliNotDB, osd: OmarchyOSD) -> None:
    ctrl = AudioCaptureController(cfg)
    st = ctrl.status()
    voice_state = VoiceState(st.get("state", VoiceState.IDLE.value))
    started_at = int(st.get("recording_started_at", 0) or 0)
    osd.write_desktop_state(
        db.build_desktop_state(voice_state=voice_state, recording_started_at=started_at)
    )


def _cmd_record(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    cfg.paths.ensure_dirs()
    ctrl = AudioCaptureController(cfg)
    db = SesliNotDB(cfg.paths.db_path)
    osd = OmarchyOSD(cfg)
    action = args.record_action

    if action == "status":
        st = ctrl.status()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if action == "toggle":
        st = ctrl.status()
        action = "stop" if st.get("state") in {"recording", "paused"} else "start"

    if action == "pause-toggle":
        st = ctrl.status()
        if st.get("state") == "recording":
            action = "pause"
        elif st.get("state") == "paused":
            action = "resume"
        else:
            return 0

    try:
        if action == "start":
            ctrl.start()
            osd.show("Dinleniyor...", icon="microphone", duration_ms=0)
            _sync_capture_to_desktop(cfg, db, osd)
            print("Kayıt başladı.")
            return 0

        if action == "pause":
            ctrl.pause()
            osd.show("Duraklatıldı", icon="󰏤", duration_ms=0)
            _sync_capture_to_desktop(cfg, db, osd)
            print("Kayıt duraklatıldı.")
            return 0

        if action == "resume":
            ctrl.resume()
            osd.show("Dinleniyor...", icon="microphone", duration_ms=0)
            _sync_capture_to_desktop(cfg, db, osd)
            print("Kayıt devam ediyor.")
            return 0

        if action == "cancel":
            ctrl.cancel()
            osd.close()
            _sync_capture_to_desktop(cfg, db, osd)
            print("Kayıt iptal edildi.")
            return 0

        if action == "stop":
            wav_path = ctrl.stop()
            pipeline = SesliNotPipeline(cfg, db=db, osd=osd)
            result = asyncio.run(
                pipeline.ingest_wav(
                    wav_path,
                    force_research=bool(getattr(args, "research", False)),
                    run_stage2_inline=bool(getattr(args, "inline_route", False)),
                )
            )
            if result.get("empty"):
                print("Konuşma algılanmadı.")
                return 0
            print(f"Not kaydedildi: {result['vault_rel_path']}")
            if result.get("job"):
                job = result["job"]
                print(f"Araştırma işi: {job.job_id} ({job.status.value})")
            return 0
    except CaptureError as exc:
        print(f"Kayıt hatası: {exc}", file=sys.stderr)
        osd.close()
        _sync_capture_to_desktop(cfg, db, osd)
        return 1

    return 2


def _cmd_note(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("Boş metin verilemez.", file=sys.stderr)
        return 2

    pipeline = SesliNotPipeline(cfg)
    result = asyncio.run(
        pipeline.ingest_transcript(
            text,
            stt_model="manual-cli",
            force_research=args.research,
            run_stage2_inline=args.run_now or args.inline_route,
            depth="quick" if args.quick else "deep",
        )
    )
    print(f"Not kaydedildi: {result['vault_rel_path']}")
    if result["masked_secrets"]:
        print(f"Uyarı: maskelenen sırlar: {', '.join(result['masked_secrets'])}")

    job = result.get("job")
    if job:
        print(f"Araştırma işi: {job.job_id} | durum={job.status.value} | konu={job.topic}")
        if args.run_now and job.status == JobStatus.QUEUED:
            return _run_job_foreground(cfg, pipeline, job.job_id)
    return 0


def _cmd_transcribe(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    wav_path = Path(args.wav).expanduser()
    if not wav_path.is_file():
        print(f"Ses dosyası bulunamadı: {wav_path}", file=sys.stderr)
        return 2

    pipeline = SesliNotPipeline(cfg)
    result = asyncio.run(
        pipeline.ingest_wav(
            wav_path,
            force_research=args.research,
            run_stage2_inline=args.run_now or args.inline_route,
        )
    )
    if result.get("empty"):
        print("Konuşma algılanmadı.")
        return 0
    print(f"Not kaydedildi: {result['vault_rel_path']}")
    job = result.get("job")
    if job:
        print(f"Araştırma işi: {job.job_id} | durum={job.status.value}")
        if args.run_now and job.status == JobStatus.QUEUED:
            return _run_job_foreground(cfg, pipeline, job.job_id)
    return 0


def _run_job_foreground(
    cfg: SesliNotConfig, pipeline: SesliNotPipeline, job_id: str
) -> int:
    job = pipeline.db.get_job(job_id)
    if job is None:
        print(f"İş bulunamadı: {job_id}", file=sys.stderr)
        return 1

    pipeline.db.update_job_status(job_id, JobStatus.RUNNING, phase=ResearchPhase.VAULT_SCAN)
    notifier = OmarchyNotifier(cfg)
    notif_id = notifier.notify_research_started(job_id, job.topic)
    pipeline.db.update_job_status(job_id, JobStatus.RUNNING, notif_id=notif_id)

    def _on_phase(phase: ResearchPhase, detail: str) -> None:
        label = detail or PHASE_LABELS_TR.get(phase, phase.value)
        print(f"  [{phase.value}] {label}", flush=True)
        pipeline.db.update_job_phase(job_id, phase, label)
        notifier.notify_research_phase(notif_id, job_id, job.topic, label)
        pipeline.osd.write_desktop_state(pipeline.db.build_desktop_state())

    try:
        res = asyncio.run(pipeline.execute_research_job(job, on_phase=_on_phase))
        pipeline.db.update_job_status(
            job_id,
            JobStatus.SUCCEEDED,
            phase=ResearchPhase.COMPLETED,
            phase_detail=PHASE_LABELS_TR[ResearchPhase.COMPLETED],
            report_rel_path=res.report_rel_path,
            obsidian_uri=res.obsidian_uri,
            source_count=res.source_count,
            finished=True,
        )
        notifier.notify_research_completed(
            notif_id, job.topic, res.obsidian_uri, res.source_count
        )
        pipeline.osd.write_desktop_state(pipeline.db.build_desktop_state())
        print(f"Rapor hazır: {res.report_rel_path} ({res.source_count} kaynak)")
        print(f"Obsidian URI: {res.obsidian_uri}")
        return 0
    except Exception as exc:
        pipeline.db.update_job_status(
            job_id,
            JobStatus.FAILED,
            phase=ResearchPhase.FAILED,
            error_message=str(exc),
            finished=True,
        )
        notifier.notify_research_failed(notif_id, job.topic, str(exc))
        pipeline.osd.write_desktop_state(pipeline.db.build_desktop_state())
        print(f"Araştırma başarısız: {exc}", file=sys.stderr)
        return 1


def _cmd_research(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    topic = " ".join(args.topic).strip()
    if not topic:
        print("Araştırma konusu boş olamaz.", file=sys.stderr)
        return 2

    pipeline = SesliNotPipeline(cfg)
    result = asyncio.run(
        pipeline.ingest_transcript(
            f"{topic} konusunu araştır.",
            stt_model="manual-research",
            force_research=True,
            depth="quick" if args.quick else "deep",
        )
    )
    job = result["job"]
    print(f"Sesli not: {result['vault_rel_path']}")
    print(f"Araştırma oturumu: {job.job_id} ({job.topic})")
    if args.run_now:
        return _run_job_foreground(cfg, pipeline, job.job_id)
    print("İşi arka planda yürütmek için `seslinotd` çalışıyor olmalı (veya `--run-now` kullanın).")
    return 0


def _cmd_status(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    cfg.paths.ensure_dirs()
    db = SesliNotDB(cfg.paths.db_path)
    ctrl = AudioCaptureController(cfg)
    st = ctrl.status()
    state = db.build_desktop_state(
        voice_state=VoiceState(st.get("state", "idle")),
        recording_started_at=int(st.get("recording_started_at", 0) or 0),
    )
    notes = db.list_recent_notes(limit=5)
    jobs = db.list_jobs(limit=10)

    payload = {
        "vault": str(cfg.vault.path),
        "desktop_state": state.model_dump(),
        "recent_notes": [
            {"id": n.note_id, "title": n.title, "path": n.vault_rel_path, "intent": n.intent.value}
            for n in notes
        ],
        "recent_jobs": [
            {
                "id": j.job_id,
                "topic": j.topic,
                "status": j.status.value,
                "phase": j.phase.value,
                "report": j.report_rel_path,
            }
            for j in jobs
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Kasa: {cfg.vault.path}")
    print(f"Ses durumu: {state.voiceState}")
    print(
        f"Aktif araştırmalar: {len(state.activeSessions)} | "
        f"Onay bekleyen: {len(state.pendingConfirm)} | "
        f"Çevrimdışı kuyruk: {state.queuedOfflineCount}"
    )
    if notes:
        print("\nSon notlar:")
        for n in notes:
            print(f"  - [{n.intent.value}] {n.title} -> {n.vault_rel_path}")
    if jobs:
        print("\nSon araştırma işleri:")
        for j in jobs[:5]:
            print(f"  - {j.job_id} [{j.status.value}/{j.phase.value}] {j.topic}")
    return 0


def _cmd_doctor(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    checks: dict[str, object] = {}
    for tool in (
        "pw-record",
        "pw-play",
        "pactl",
        "ffmpeg",
        "voxtype",
        "whisper-server",
        "claude",
        "codex",
        "agy",
        "opencode",
        "omarchy-shell",
        "omarchy-notification-send",
        "systemd-run",
        "rg",
    ):
        checks[f"bin:{tool}"] = shutil.which(tool) is not None

    checks["vault_exists"] = cfg.vault.path.is_dir()
    checks["hafiza_cli_exists"] = (cfg.vault.path / cfg.vault.hafiza_tools_dir / "hafiza.py").is_file()

    # STT HTTP health check (non-fatal)
    stt_ok = False
    try:
        base = cfg.audio.stt_endpoint.rsplit("/v1/", 1)[0]
        r = httpx.get(f"{base}/health", timeout=1.0)
        stt_ok = r.status_code < 500
    except Exception:
        stt_ok = False
    checks["stt_http_online"] = stt_ok

    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0

    print("SesliNot Ortam Sağlık Denetimi:")
    for key, ok in checks.items():
        mark = "OK  " if ok else "YOK "
        print(f"  [{mark}] {key}")
    return 0


def _cmd_memory_approve(args: argparse.Namespace, cfg: SesliNotConfig) -> int:
    db = SesliNotDB(cfg.paths.db_path)
    note = db.get_note(args.note_id)
    if note is None:
        print(f"Not bulunamadı: {args.note_id}", file=sys.stderr)
        return 1
    bridge = HafizaBridge(cfg)
    result = bridge.promote_user_approved_memory(
        statement=args.statement,
        subject_key=args.subject_key,
        source_rel_path=note.vault_rel_path,
        evidence=args.evidence or note.raw_transcript[:200],
        scope=args.scope or note.scope,
        category=args.category,
        user_approved=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seslinot",
        description="SesliNot — Yerel GPU sesli not ve çok-ajanlı derin araştırma CLI",
    )
    p.add_argument("--config", type=Path, default=None, help="Özel config.toml yolu")
    sub = p.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Sesli not kaydını yönet")
    rec.add_argument(
        "record_action",
        choices=["toggle", "start", "stop", "pause", "resume", "pause-toggle", "cancel", "status"],
    )
    rec.add_argument("--research", action="store_true", help="Durunca zorunlu araştırma başlat")
    rec.add_argument("--inline-route", action="store_true", help="Stage 2 router'ı anında çalıştır")

    note = sub.add_parser("note", help="Metin olarak sesli not akışını çalıştır")
    note.add_argument("text", nargs="+", help="Not metni")
    note.add_argument("--research", action="store_true", help="Araştırmayı zorla başlat")
    note.add_argument("--quick", action="store_true", help="Hızlı araştırma derinliği")
    note.add_argument("--inline-route", action="store_true", help="Stage 2 router'ı anında çalıştır")
    note.add_argument("--run-now", action="store_true", help="Araştırma açılırsa ön planda çalıştır")

    tr = sub.add_parser("transcribe", help="Mevcut WAV dosyasını nota dönüştür")
    tr.add_argument("wav", help="WAV dosyası yolu")
    tr.add_argument("--research", action="store_true")
    tr.add_argument("--inline-route", action="store_true")
    tr.add_argument("--run-now", action="store_true")

    res = sub.add_parser("research", help="Doğrudan çok-ajanlı araştırma oturumu başlat")
    res.add_argument("topic", nargs="+", help="Araştırma konusu")
    res.add_argument("--quick", action="store_true")
    res.add_argument("--run-now", action="store_true", help="Ön planda çalıştır ve ilerlemeyi yazdır")

    st = sub.add_parser("status", help="Aktif durum, son notlar ve işler")
    st.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor", help="Yerel araçlar, STT ve Hafıza kasası sağlık kontrolü")
    doc.add_argument("--json", action="store_true")

    mem = sub.add_parser("memory", help="Kullanıcı onaylı Hafıza OS aday/terfi köprüsü")
    mem_sub = mem.add_subparsers(dest="memory_cmd", required=True)
    app = mem_sub.add_parser("approve", help="Sesli nottan kalıcı tercih/kararı onayla")
    app.add_argument("note_id")
    app.add_argument("--statement", required=True)
    app.add_argument("--subject-key", required=True)
    app.add_argument("--evidence", default="")
    app.add_argument("--scope", default="")
    app.add_argument("--category", default="preference")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "record":
        return _cmd_record(args, cfg)
    if args.command == "note":
        return _cmd_note(args, cfg)
    if args.command == "transcribe":
        return _cmd_transcribe(args, cfg)
    if args.command == "research":
        return _cmd_research(args, cfg)
    if args.command == "status":
        return _cmd_status(args, cfg)
    if args.command == "doctor":
        return _cmd_doctor(args, cfg)
    if args.command == "memory" and args.memory_cmd == "approve":
        return _cmd_memory_approve(args, cfg)
    return 2


def ctl_main(argv: Sequence[str] | None = None) -> int:
    """`seslinot-ctl` giriş noktası (Omarchy bar/bildirim aksiyonları için)."""
    p = argparse.ArgumentParser(prog="seslinot-ctl", description="SesliNot araştırma iş kontrolü")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("action", choices=["confirm", "dismiss", "cancel", "open"])
    p.add_argument("job_id")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    daemon = SesliNotDaemon(cfg)
    if args.action == "confirm":
        ok = daemon.confirm_job(args.job_id)
    elif args.action == "dismiss":
        ok = daemon.dismiss_job(args.job_id)
    elif args.action == "cancel":
        ok = daemon.cancel_job(args.job_id)
    elif args.action == "open":
        job = daemon.db.get_job(args.job_id)
        if not job or not job.report_rel_path:
            return 1
        uri = job.obsidian_uri or make_obsidian_uri(cfg.vault.vault_name, job.report_rel_path)
        print(uri)
        return 0
    else:
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
