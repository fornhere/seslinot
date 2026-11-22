"""SesliNot komut satırı arayüzü (`seslinot` ve `seslinot-ctl`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

import httpx

from seslinot.audio import AudioCaptureController, CaptureError
from seslinot.config import SesliNotConfig, default_config_path, load_config
from seslinot.db import SesliNotDB
from seslinot.desktop import OmarchyNotifier, OmarchyOSD, SesliNotDaemon, make_obsidian_uri
from seslinot.models import (
    PHASE_LABELS_TR,
    JobStatus,
    ResearchPhase,
    VoiceState,
)
from seslinot.pipeline import SesliNotPipeline
from seslinot.platform.os_compat import (
    current_platform,
    detect_obsidian_vaults,
    is_hafiza_os_vault,
)
from seslinot.vault import HafizaBridge, VaultWriter


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
    cfg: SesliNotConfig,
    pipeline: SesliNotPipeline,
    job_id: str,
    *,
    auto_save: bool | None = None,
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
        res = asyncio.run(
            pipeline.execute_research_job(job, on_phase=_on_phase, auto_save=auto_save)
        )
        if res.awaiting_approval:
            nid = notifier.notify_research_ready_for_approval(
                notif_id,
                job_id,
                job.topic,
                res.preview_path,
                res.source_count,
                res.confidence_label,
            )
            pipeline.db.update_job_status(
                job_id,
                JobStatus.AWAITING_APPROVAL,
                phase=ResearchPhase.AWAITING_APPROVAL,
                phase_detail=PHASE_LABELS_TR[ResearchPhase.AWAITING_APPROVAL],
                source_count=res.source_count,
                session_dir=res.session_dir or job.session_dir,
                notif_id=nid or notif_id,
            )
            pipeline.osd.write_desktop_state(pipeline.db.build_desktop_state())
            print(f"\nRapor hazır ve onayınızı bekliyor ({res.source_count} doğrulanmış kaynak, güven: {res.confidence_label}).")
            print(f"Önizleme : {res.preview_path}")
            print(f"Kaydet   : seslinot-ctl approve-report {job_id}")
            print(f"Reddet   : seslinot-ctl reject-report {job_id}")
            return 0

        pipeline.db.update_job_status(
            job_id,
            JobStatus.SUCCEEDED,
            phase=ResearchPhase.COMPLETED,
            phase_detail=PHASE_LABELS_TR[ResearchPhase.COMPLETED],
            report_rel_path=res.report_rel_path,
            obsidian_uri=res.obsidian_uri,
            source_count=res.source_count,
            session_dir=res.session_dir or job.session_dir,
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
        return _run_job_foreground(
            cfg,
            pipeline,
            job.job_id,
            auto_save=True if getattr(args, "auto_save", False) else None,
        )
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
    plat = current_platform()
    checks: dict[str, object] = {
        "platform": plat,
        "capture_backend": cfg.audio.capture_backend,
        "stt_provider": cfg.audio.stt_provider,
        "hafiza_os_detected": is_hafiza_os_vault(cfg.vault.path),
    }

    tools: list[str] = ["ffmpeg", "claude", "codex", "agy", "opencode", "rg"]
    if plat == "linux":
        tools = [
            "pw-record",
            "pw-play",
            "pactl",
            "voxtype",
            "whisper-server",
            "omarchy-shell",
            "omarchy-notification-send",
            "systemd-run",
            *tools,
        ]
    elif plat == "macos":
        tools = ["afplay", "osascript", "terminal-notifier", "whisper-server", *tools]
    else:
        tools = ["powershell", "whisper-server", *tools]

    for tool in tools:
        checks[f"bin:{tool}"] = shutil.which(tool) is not None

    # Opsiyonel çapraz-platform masaüstü paketleri
    for pkg in ("sounddevice", "pystray", "pynput"):
        try:
            __import__(pkg)
            checks[f"pkg:{pkg}"] = True
        except ImportError:
            checks[f"pkg:{pkg}"] = False

    # Ortam değişkeni varlığı (değer ASLA yazdırılmaz)
    for env_name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY"):
        checks[f"env:{env_name}"] = bool(os.environ.get(env_name, "").strip())

    checks["vault_exists"] = cfg.vault.path.is_dir()
    checks["hafiza_cli_exists"] = (cfg.vault.path / cfg.vault.hafiza_tools_dir / "hafiza.py").is_file()

    # STT HTTP health check (yalnız yerel uç noktalarda non-fatal)
    stt_ok = False
    if cfg.audio.stt_provider == "local":
        try:
            base = cfg.audio.stt_endpoint.rsplit("/v1/", 1)[0]
            r = httpx.get(f"{base}/health", timeout=1.0)
            stt_ok = r.status_code < 500
        except Exception:
            stt_ok = False
    else:
        stt_ok = True
    checks["stt_http_online"] = stt_ok

    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0

    print(f"SesliNot Ortam Sağlık Denetimi ({plat}):")
    for key, val in checks.items():
        if isinstance(val, bool):
            mark = "OK  " if val else "YOK "
            print(f"  [{mark}] {key}")
        else:
            print(f"  [INFO] {key}: {val}")
    return 0


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _cmd_init(args: argparse.Namespace) -> int:
    """Linux, macOS ve Windows için etkileşimli / otomatik SesliNot kurulum sihirbazı."""
    plat = current_platform()
    cfg_path = args.config or default_config_path()

    if cfg_path.is_file() and not args.force:
        print(f"Yapılandırma zaten mevcut: {cfg_path}")
        print("Üzerine yazmak için `--force` bayrağını kullanın.")
        return 1

    discovered = detect_obsidian_vaults()
    interactive = (not args.non_interactive) and sys.stdin.isatty()

    # 1. Obsidian kasası seçimi
    vault_path: Path
    if args.vault:
        vault_path = Path(args.vault).expanduser().resolve()
    elif discovered:
        if interactive and len(discovered) > 1:
            print("Keşfedilen Obsidian kasaları:")
            for idx, v in enumerate(discovered, start=1):
                tag = " [Hafıza OS]" if v.is_hafiza_os else ""
                active = " (açık)" if v.is_open else ""
                print(f"  {idx}) {v.name} — {v.path}{tag}{active}")
            choice = input("Kullanılacak kasa numarası veya özel yol [1]: ").strip()
            if not choice or choice == "1":
                vault_path = discovered[0].path
            elif choice.isdigit() and 1 <= int(choice) <= len(discovered):
                vault_path = discovered[int(choice) - 1].path
            else:
                vault_path = Path(choice).expanduser().resolve()
        else:
            vault_path = discovered[0].path
    else:
        default_v = Path.home() / "Obsidian" / "SesliNot"
        if interactive:
            raw = input(f"Obsidian kasa yolu [{default_v}]: ").strip()
            vault_path = Path(raw).expanduser().resolve() if raw else default_v
        else:
            vault_path = default_v

    vault_path.mkdir(parents=True, exist_ok=True)
    hafiza_detected = is_hafiza_os_vault(vault_path)

    # 2. STT sağlayıcısı seçimi
    stt_provider = args.stt
    if stt_provider == "auto":
        if plat == "linux" and (shutil.which("voxtype") or shutil.which("whisper-server")):
            stt_provider = "local"
        elif os.environ.get("GROQ_API_KEY"):
            stt_provider = "groq"
        elif os.environ.get("OPENAI_API_KEY"):
            stt_provider = "openai"
        elif interactive:
            print("\nSes-metin (STT) profili:")
            print("  1) local  — Yerel whisper-server / voxtype (GPU önerilir)")
            print("  2) groq   — Groq Cloud Whisper (ultra hızlı, GROQ_API_KEY)")
            print("  3) openai — OpenAI Whisper API (OPENAI_API_KEY)")
            c = input("Seçim [1]: ").strip().lower()
            stt_provider = {"2": "groq", "groq": "groq", "3": "openai", "openai": "openai"}.get(c, "local")
        else:
            stt_provider = "local"

    # 3. Araştırma backend seçimi
    has_claude = shutil.which("claude") is not None
    has_codex = shutil.which("codex") is not None
    has_agy = shutil.which("agy") is not None
    backend_mode = args.backend
    if backend_mode == "auto":
        if has_claude or has_codex or has_agy:
            backend_mode = "cli"
        elif os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
            backend_mode = "api"
        else:
            backend_mode = "cli" if plat == "linux" else "api"

    if backend_mode == "api":
        api_provider = "anthropic" if (os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY")) else "openai"
        api_model = "claude-sonnet-4-20250514" if api_provider == "anthropic" else "gpt-4o-mini"
        router_backends = '["api"]'
        lead_backend = worker_backend = synth_backend = audit_backend = "api"
        lead_model = worker_model = synth_model = audit_model = api_model
    else:
        api_provider = "openai"
        api_model = "gpt-4o-mini"
        ordered = [b for b, ok in (("claude", has_claude), ("agy", has_agy), ("codex", has_codex)) if ok]
        if not ordered:
            ordered = ["claude", "agy", "codex"]
        router_backends = json.dumps(ordered)
        lead_backend = "claude" if has_claude else ordered[0]
        worker_backend = "codex" if has_codex else lead_backend
        synth_backend = lead_backend
        audit_backend = "codex" if (has_codex and synth_backend != "codex") else lead_backend
        lead_model = "sonnet" if lead_backend == "claude" else "gpt-6-luna"
        worker_model = "gpt-6-luna" if worker_backend == "codex" else "sonnet"
        synth_model = lead_model
        audit_model = "gpt-6-luna" if audit_backend == "codex" else "sonnet"

    capture_backend = "pipewire" if (plat == "linux" and shutil.which("pw-record")) else "sounddevice"
    use_systemd = "true" if (plat == "linux" and shutil.which("systemd-run")) else "false"
    enable_omarchy = "true" if (plat == "linux" and shutil.which("omarchy-shell")) else "false"

    toml_text = "\n".join(
        [
            "# SesliNot yapılandırması (`seslinot init` tarafından üretildi)",
            "",
            "[vault]",
            f"path = {_toml_quote(vault_path.as_posix())}",
            f"vault_name = {_toml_quote(vault_path.name)}",
            'voice_dir = "sesli-notlar"',
            'research_dir = "araştırmalar"',
            f'hafiza_os_mode = "auto"  # tespit edildi: {"evet" if hafiza_detected else "hayır"}',
            "auto_update_maps = true",
            "",
            "[audio]",
            f"capture_backend = {_toml_quote(capture_backend)}",
            f"stt_provider = {_toml_quote(stt_provider)}",
            'language = "tr"',
            'retention = "opus_30d"',
            "",
            "[router]",
            f"backends = {router_backends}",
            "auto_start_threshold = 0.80",
            "confirm_threshold = 0.55",
            "",
            "[research]",
            f"lead_backend = {_toml_quote(lead_backend)}",
            f"lead_model = {_toml_quote(lead_model)}",
            f"worker_backend = {_toml_quote(worker_backend)}",
            f"worker_model = {_toml_quote(worker_model)}",
            f"synth_backend = {_toml_quote(synth_backend)}",
            f"synth_model = {_toml_quote(synth_model)}",
            f"audit_backend = {_toml_quote(audit_backend)}",
            f"audit_model = {_toml_quote(audit_model)}",
            f"api_provider = {_toml_quote(api_provider)}",
            f"api_model = {_toml_quote(api_model)}",
            f"use_systemd_run = {use_systemd}",
            "require_save_approval = true",
            "",
            "[desktop]",
            f"enable_osd = {enable_omarchy}",
            "enable_notifications = true",
            f"enable_bar_refresh = {enable_omarchy}",
            'hotkey_toggle = "<ctrl>+<alt>+n"',
            "",
        ]
    )

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(toml_text, encoding="utf-8")

    # Kasa banka klasörlerini (.base + README) güvenle başlat
    new_cfg = load_config(cfg_path)
    new_cfg.paths.ensure_dirs()
    VaultWriter(new_cfg).ensure_bank_scaffolding()

    print(f"Kurulum tamamlandı ({plat})!")
    print(f"  Yapılandırma : {cfg_path}")
    print(f"  Obsidian kasa: {vault_path} ({'Hafıza OS' if hafiza_detected else 'Standart Obsidian'})")
    print(f"  Ses / STT    : {capture_backend} / {stt_provider}")
    print(f"  Araştırma    : {backend_mode} (onay kapısı: açık)")
    if plat == "linux":
        print("\nSonraki adım: `seslinot doctor` ve arka plan servisi için `seslinotd` çalıştırın.")
    else:
        print("\nSonraki adım: Sistem tepsisi ve global kısayol için `seslinot tray` çalıştırın.")
    return 0


def _cmd_tray(cfg: SesliNotConfig) -> int:
    from seslinot.desktop.tray import SesliNotTrayApp

    try:
        return SesliNotTrayApp(cfg).run()
    except RuntimeError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1


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
        source_path=note.vault_rel_path,
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
        description="SesliNot — Çapraz-platform sesli not ve çok-ajanlı derin araştırma CLI",
    )
    p.add_argument("--config", type=Path, default=None, help="Özel config.toml yolu")
    sub = p.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Linux/macOS/Windows için ilk kurulum sihirbazı")
    init_p.add_argument("--vault", type=str, default="", help="Obsidian kasa klasör yolu")
    init_p.add_argument("--stt", choices=["auto", "local", "groq", "openai"], default="auto")
    init_p.add_argument("--backend", choices=["auto", "cli", "api"], default="auto")
    init_p.add_argument("--non-interactive", action="store_true", help="Soru sormadan otomatik kur")
    init_p.add_argument("--force", action="store_true", help="Mevcut config.toml üzerine yaz")

    sub.add_parser("tray", help="Sistem tepsisi ve global kısayol uygulamasını başlat (Win/Mac/Linux)")

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
    res.add_argument("--auto-save", action="store_true", help="Onay beklemeden doğrudan kasaya kaydet")

    st = sub.add_parser("status", help="Aktif durum, son notlar ve işler")
    st.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor", help="Yerel araçlar, STT ve Obsidian kasası sağlık kontrolü")
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
    if args.command == "init":
        return _cmd_init(args)

    cfg = load_config(args.config)

    if args.command == "tray":
        return _cmd_tray(cfg)
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
    p.add_argument(
        "action",
        choices=["confirm", "dismiss", "cancel", "approve-report", "reject-report", "preview", "open"],
    )
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
    elif args.action == "approve-report":
        pipeline = SesliNotPipeline(cfg, db=daemon.db, osd=daemon.osd, notifier=daemon.notifier)
        res = pipeline.approve_pending_report(args.job_id)
        if res is None:
            print(f"Onay bekleyen rapor bulunamadı: {args.job_id}", file=sys.stderr)
            return 1
        print(f"Kasaya kaydedildi: {res.report_rel_path}")
        print(res.obsidian_uri)
        return 0
    elif args.action == "reject-report":
        pipeline = SesliNotPipeline(cfg, db=daemon.db, osd=daemon.osd, notifier=daemon.notifier)
        ok = pipeline.reject_pending_report(args.job_id)
        if ok:
            print(f"Rapor reddedildi (kasaya yazılmadı): {args.job_id}")
    elif args.action == "preview":
        job = daemon.db.get_job(args.job_id)
        if not job or not job.session_dir:
            return 1
        preview = Path(job.session_dir) / "preview.md"
        if not preview.is_file():
            return 1
        print(preview.read_text(encoding="utf-8"))
        return 0
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
