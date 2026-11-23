"""SesliNot Tauri v2 masaüstü uygulaması için yalnızca-localhost HTTP + SSE API sunucusu.

Güvenlik modeli:
- Yalnızca `127.0.0.1` loopback arayüzüne bağlanır.
- `/api/health` dışındaki tüm uç noktalar tek kullanımlık `Bearer <token>` ister.
- API anahtarları yalnızca `secrets.env` (0600) dosyasına yazılır; GET yanıtlarında
  asla düz metin olarak dönülmez (yalnızca `configured: bool`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from seslinot import __version__
from seslinot.audio import AudioCaptureController, CaptureError
from seslinot.config import (
    ALLOWED_SECRET_KEYS,
    SesliNotConfig,
    default_config_path,
    load_config,
    save_secrets_env,
    write_config_toml,
)
from seslinot.desktop.daemon import SesliNotDaemon
from seslinot.desktop.notify import make_obsidian_uri
from seslinot.models import JobStatus, VoiceState
from seslinot.pipeline import SesliNotPipeline
from seslinot.platform.os_compat import (
    current_platform,
    detect_obsidian_vaults,
    is_hafiza_os_vault,
    open_target,
)
from seslinot.vault import VaultWriter

logger = logging.getLogger(__name__)


class _ServerState:
    """Sunucu çalışma zamanı durumunu, canlı boru hattını ve SSE abonelerini yönetir."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        config_path: Path,
        api_token: str,
        pipeline: SesliNotPipeline | None = None,
        daemon: SesliNotDaemon | None = None,
        start_daemon: bool = True,
    ) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.api_token = api_token
        self.start_daemon = start_daemon
        self.pipeline = pipeline or SesliNotPipeline(cfg)
        self.daemon = daemon or SesliNotDaemon(
            cfg,
            db=self.pipeline.db,
            osd=self.pipeline.osd,
            notifier=self.pipeline.notifier,
            route_handler=self.pipeline.route_job,
            research_handler=self.pipeline.execute_research_job,
        )
        self._daemon_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._action_lock = asyncio.Lock()

    def reload_config(self, new_cfg: SesliNotConfig) -> None:
        self.cfg = new_cfg
        self.cfg.paths.ensure_dirs()
        self.pipeline = SesliNotPipeline(new_cfg)
        self.daemon.cfg = new_cfg
        self.daemon.db = self.pipeline.db
        self.daemon.osd = self.pipeline.osd
        self.daemon.notifier = self.pipeline.notifier
        self.daemon._route_handler = self.pipeline.route_job
        self.daemon._research_handler = self.pipeline.execute_research_job

    def build_snapshot(self) -> dict[str, Any]:
        ctrl = AudioCaptureController(self.cfg)
        cap = ctrl.status()
        raw_voice = str(cap.get("state") or VoiceState.IDLE.value)
        try:
            voice_state = VoiceState(raw_voice)
        except ValueError:
            voice_state = VoiceState.IDLE
        started_at = int(cap.get("recording_started_at") or 0)

        desktop = self.pipeline.db.build_desktop_state(
            voice_state=voice_state,
            recording_started_at=started_at,
        )
        notes = self.pipeline.db.list_recent_notes(limit=25)
        jobs = self.pipeline.db.list_jobs(limit=25)

        return {
            "version": __version__,
            "platform": current_platform(),
            "needsOnboarding": not self.config_path.is_file(),
            "vault": {
                "path": str(self.cfg.vault.path),
                "name": self.cfg.vault.vault_name,
                "exists": self.cfg.vault.path.is_dir(),
                "hafizaOs": is_hafiza_os_vault(self.cfg.vault.path),
            },
            "desktop": desktop.model_dump(mode="json"),
            "notes": [
                {
                    "id": n.note_id,
                    "title": n.title,
                    "transcript": n.raw_transcript,
                    "relPath": n.vault_rel_path,
                    "obsidianUri": make_obsidian_uri(self.cfg.vault.vault_name, n.vault_rel_path)
                    if n.vault_rel_path
                    else "",
                    "intent": n.intent.value,
                    "status": n.status.value,
                    "durationSecs": n.duration_secs,
                    "relatedResearch": n.related_research,
                    "createdAt": n.created_at,
                }
                for n in notes
            ],
            "jobs": [
                {
                    "id": j.job_id,
                    "noteId": j.note_id,
                    "topic": j.topic,
                    "status": j.status.value,
                    "phase": j.phase.value,
                    "phaseDetail": j.phase_detail,
                    "confidence": round(j.confidence, 2),
                    "sourceCount": j.source_count,
                    "reportRelPath": j.report_rel_path,
                    "obsidianUri": j.obsidian_uri,
                    "error": j.error_message,
                    "createdAt": j.created_at,
                    "startedAt": j.started_at,
                    "finishedAt": j.finished_at,
                }
                for j in jobs
            ],
        }

    def notify_subscribers(self) -> None:
        if not self._subscribers:
            return
        payload = json.dumps(self.build_snapshot(), ensure_ascii=False)
        stale: list[asyncio.Queue[str]] = []
        for q in list(self._subscribers):
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(payload)
            except Exception:
                stale.append(q)
        for q in stale:
            self._subscribers.discard(q)


def _check_auth(request: Request, state: _ServerState) -> JSONResponse | None:
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.query_params.get("token"):
        token = request.query_params["token"].strip()
    if not token or not secrets.compare_digest(token, state.api_token):
        return JSONResponse({"ok": False, "error": "Yetkisiz erişim"}, status_code=401)
    return None


def _build_doctor_payload(cfg: SesliNotConfig) -> dict[str, Any]:
    plat = current_platform()
    tools: list[str] = ["ffmpeg", "claude", "codex", "agy", "opencode", "rg"]
    if plat == "linux":
        tools = ["pw-record", "pw-play", "pactl", "voxtype", "whisper-server", "omarchy-shell", *tools]
    elif plat == "macos":
        tools = ["afplay", "osascript", "terminal-notifier", "whisper-server", *tools]
    else:
        tools = ["powershell", "whisper-server", *tools]

    bins = {tool: shutil.which(tool) is not None for tool in tools}
    pkgs: dict[str, bool] = {}
    for pkg in ("sounddevice", "pystray", "pynput"):
        try:
            __import__(pkg)
            pkgs[pkg] = True
        except ImportError:
            pkgs[pkg] = False
    envs = {k: bool(os.environ.get(k, "").strip()) for k in ALLOWED_SECRET_KEYS}
    return {
        "platform": plat,
        "vaultExists": cfg.vault.path.is_dir(),
        "hafizaOsDetected": is_hafiza_os_vault(cfg.vault.path),
        "captureBackend": cfg.audio.capture_backend,
        "sttProvider": cfg.audio.stt_provider,
        "bins": bins,
        "packages": pkgs,
        "secrets": envs,
    }


def _serialize_settings(cfg: SesliNotConfig, cfg_path: Path) -> dict[str, Any]:
    return {
        "configPath": str(cfg_path),
        "vault": {
            "path": str(cfg.vault.path),
            "vault_name": cfg.vault.vault_name,
            "voice_dir": cfg.vault.voice_dir,
            "research_dir": cfg.vault.research_dir,
            "hafiza_os_mode": cfg.vault.hafiza_os_mode,
            "personal_terms": list(cfg.vault.personal_terms),
            "auto_update_maps": cfg.vault.auto_update_maps,
        },
        "audio": {
            "capture_backend": cfg.audio.capture_backend,
            "stt_provider": cfg.audio.stt_provider,
            "stt_endpoint": cfg.audio.stt_endpoint,
            "stt_model": cfg.audio.stt_model,
            "stt_api_key_env": cfg.audio.stt_api_key_env,
            "language": cfg.audio.language,
            "duck_enabled": cfg.audio.duck_enabled,
            "cues_enabled": cfg.audio.cues_enabled,
            "retention": cfg.audio.retention,
        },
        "router": {
            "backends": list(cfg.router.backends),
            "auto_start_threshold": cfg.router.auto_start_threshold,
            "confirm_threshold": cfg.router.confirm_threshold,
        },
        "research": {
            "lead_backend": cfg.research.lead_backend,
            "lead_model": cfg.research.lead_model,
            "worker_backend": cfg.research.worker_backend,
            "worker_model": cfg.research.worker_model,
            "synth_backend": cfg.research.synth_backend,
            "synth_model": cfg.research.synth_model,
            "audit_backend": cfg.research.audit_backend,
            "audit_model": cfg.research.audit_model,
            "api_provider": cfg.research.api_provider,
            "api_base_url": cfg.research.api_base_url,
            "api_model": cfg.research.api_model,
            "search_provider": cfg.research.search_provider,
            "max_subagents": cfg.research.max_subagents,
            "require_save_approval": cfg.research.require_save_approval,
        },
        "desktop": {
            "enable_notifications": cfg.desktop.enable_notifications,
            "hotkey_toggle": cfg.desktop.hotkey_toggle,
        },
        "secrets": {k: bool(os.environ.get(k, "").strip()) for k in ALLOWED_SECRET_KEYS},
    }


def create_app(
    cfg: SesliNotConfig,
    *,
    config_path: Path | None = None,
    api_token: str | None = None,
    start_daemon: bool = True,
    pipeline: SesliNotPipeline | None = None,
    daemon: SesliNotDaemon | None = None,
) -> Starlette:
    """SesliNot lokal Starlette HTTP/SSE uygulamasını üretir."""
    cfg_path = config_path or default_config_path()
    token = api_token or secrets.token_urlsafe(32)
    state = _ServerState(
        cfg,
        config_path=cfg_path,
        api_token=token,
        pipeline=pipeline,
        daemon=daemon,
        start_daemon=start_daemon,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        if state.start_daemon:
            state._daemon_task = asyncio.create_task(state.daemon.run_forever())
        try:
            yield
        finally:
            if state._daemon_task is not None:
                state.daemon.request_stop()
                try:
                    await asyncio.wait_for(state._daemon_task, timeout=3.0)
                except (TimeoutError, asyncio.CancelledError):
                    state._daemon_task.cancel()

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, "platform": current_platform()})

    async def get_state(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        return JSONResponse(state.build_snapshot())

    async def events(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err

        q: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        state._subscribers.add(q)

        async def _stream():
            last_sent = ""
            try:
                initial = json.dumps(state.build_snapshot(), ensure_ascii=False)
                last_sent = initial
                yield f"event: state\ndata: {initial}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        data = json.dumps(state.build_snapshot(), ensure_ascii=False)
                    if data != last_sent:
                        last_sent = data
                        yield f"event: state\ndata: {data}\n\n"
                    else:
                        yield ": keepalive\n\n"
            finally:
                state._subscribers.discard(q)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def record_action(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        action = request.path_params["action"]
        if action not in {"toggle", "start", "stop", "pause-toggle", "cancel"}:
            return JSONResponse({"ok": False, "error": f"Geçersiz kayıt aksiyonu: {action}"}, status_code=400)

        body: dict[str, Any] = {}
        try:
            raw = await request.body()
            if raw:
                body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        force_research = bool(body.get("force_research", False))

        async with state._action_lock:
            try:
                if action == "toggle":
                    res = await state.pipeline.toggle_recording(
                        force_research=force_research, run_stage2_inline=False
                    )
                elif action == "start":
                    res = state.pipeline.start_recording()
                elif action == "stop":
                    res = await state.pipeline.stop_and_ingest(
                        force_research=force_research, run_stage2_inline=False
                    )
                elif action == "pause-toggle":
                    res = state.pipeline.pause_or_resume_recording()
                else:
                    res = state.pipeline.cancel_recording()
            except CaptureError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
            except Exception as exc:
                logger.exception("Kayıt aksiyonu hatası (%s)", action)
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        state.notify_subscribers()
        # JSON serileştirilebilir sadeleştirme
        safe_res: dict[str, Any] = {}
        for k, v in res.items():
            if hasattr(v, "model_dump"):
                safe_res[k] = v.model_dump(mode="json")
            elif isinstance(v, Path):
                safe_res[k] = str(v)
            else:
                safe_res[k] = v
        return JSONResponse({"ok": True, "result": safe_res, "snapshot": state.build_snapshot()})

    async def create_note(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Geçersiz JSON"}, status_code=400)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Not metni boş olamaz"}, status_code=400)

        res = await state.pipeline.ingest_transcript(
            text,
            force_research=bool(body.get("force_research", False)),
            run_stage2_inline=False,
            depth="quick" if body.get("quick") else "deep",
        )
        state.notify_subscribers()
        job = res.get("job")
        return JSONResponse(
            {
                "ok": True,
                "noteId": res["note"].note_id,
                "vaultRelPath": res["vault_rel_path"],
                "obsidianUri": res["obsidian_uri"],
                "jobId": job.job_id if job else None,
                "snapshot": state.build_snapshot(),
            }
        )

    async def create_research(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Geçersiz JSON"}, status_code=400)
        topic = str(body.get("topic") or "").strip()
        if not topic:
            return JSONResponse({"ok": False, "error": "Araştırma konusu boş olamaz"}, status_code=400)
        res = await state.pipeline.ingest_transcript(
            topic,
            force_research=True,
            run_stage2_inline=False,
            depth="quick" if body.get("quick") else "deep",
        )
        state.notify_subscribers()
        job = res.get("job")
        return JSONResponse(
            {
                "ok": True,
                "jobId": job.job_id if job else None,
                "noteId": res["note"].note_id,
                "snapshot": state.build_snapshot(),
            }
        )

    async def job_preview(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        job_id = request.path_params["job_id"]
        job = state.pipeline.db.get_job(job_id)
        if job is None or not job.session_dir:
            return JSONResponse({"ok": False, "error": "İş veya oturum bulunamadı"}, status_code=404)
        sess = Path(job.session_dir)
        preview_file = sess / "preview.md"
        staged_file = sess / "pending_report.json"
        if not preview_file.is_file():
            return JSONResponse({"ok": False, "error": "Önizleme dosyası henüz hazır değil"}, status_code=404)

        meta: dict[str, Any] = {}
        if staged_file.is_file():
            try:
                raw = json.loads(staged_file.read_text(encoding="utf-8"))
                meta = {
                    "summary": raw.get("summary", ""),
                    "sourceCount": int(raw.get("source_count", job.source_count)),
                    "verifiedQuotes": int(raw.get("verified_quotes", 0)),
                    "confidence": raw.get("confidence", "orta"),
                    "auditStatus": raw.get("audit_status", "passed"),
                    "title": (raw.get("report") or {}).get("title") or job.topic,
                    "concepts": (raw.get("report") or {}).get("concepts") or [],
                }
            except (OSError, ValueError, json.JSONDecodeError):
                meta = {}
        return JSONResponse(
            {
                "ok": True,
                "jobId": job_id,
                "status": job.status.value,
                "previewPath": str(preview_file),
                "markdown": preview_file.read_text(encoding="utf-8"),
                "meta": meta,
            }
        )

    async def job_action(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        job_id = request.path_params["job_id"]
        action = request.path_params["action"]

        if action == "confirm":
            ok = state.daemon.confirm_job(job_id)
            extra: dict[str, Any] = {}
        elif action == "dismiss":
            ok = state.daemon.dismiss_job(job_id)
            extra = {}
        elif action == "cancel":
            ok = state.daemon.cancel_job(job_id)
            extra = {}
        elif action == "approve-report":
            res = state.pipeline.approve_pending_report(job_id)
            ok = res is not None
            extra = (
                {"reportRelPath": res.report_rel_path, "obsidianUri": res.obsidian_uri}
                if res
                else {}
            )
        elif action == "reject-report":
            ok = state.pipeline.reject_pending_report(job_id)
            extra = {}
        else:
            return JSONResponse({"ok": False, "error": f"Bilinmeyen iş aksiyonu: {action}"}, status_code=400)

        state.notify_subscribers()
        if not ok:
            return JSONResponse({"ok": False, "error": f"İşlem uygulanamadı ({action}: {job_id})"}, status_code=409)
        return JSONResponse({"ok": True, **extra, "snapshot": state.build_snapshot()})

    async def open_item(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Geçersiz JSON"}, status_code=400)
        target = str(body.get("target") or "").strip()
        if not target:
            return JSONResponse({"ok": False, "error": "Hedef boş"}, status_code=400)
        # Güvenlik: yalnızca obsidian:// URI, mevcut kasa veya oturum dizini içi açılabilir
        allowed = False
        if target.startswith("obsidian://open?"):
            allowed = True
        else:
            try:
                resolved = Path(target).expanduser().resolve()
                vault_root = state.cfg.vault.path.expanduser().resolve()
                sessions_root = state.cfg.paths.sessions_dir.expanduser().resolve()
                if (
                    resolved == vault_root
                    or resolved.is_relative_to(vault_root)
                    or resolved.is_relative_to(sessions_root)
                ):
                    allowed = True
            except OSError:
                allowed = False
        if not allowed:
            return JSONResponse({"ok": False, "error": "İzin verilmeyen hedef yolu"}, status_code=403)
        opened = open_target(target)
        return JSONResponse({"ok": opened})

    async def list_vaults(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        vaults = [
            {
                "name": v.name,
                "path": str(v.path),
                "isOpen": v.is_open,
                "isHafizaOs": v.is_hafiza_os,
            }
            for v in detect_obsidian_vaults()
        ]
        return JSONResponse({"ok": True, "vaults": vaults})

    async def get_doctor(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        return JSONResponse({"ok": True, "doctor": _build_doctor_payload(state.cfg)})

    async def get_settings(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        return JSONResponse({"ok": True, "settings": _serialize_settings(state.cfg, state.config_path)})

    async def update_settings(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Geçersiz JSON"}, status_code=400)

        secrets_patch = body.get("secrets")
        if isinstance(secrets_patch, dict):
            save_secrets_env(secrets_patch, state.config_path)

        data = state.cfg.model_dump()
        for section in ("vault", "audio", "router", "research", "desktop"):
            patch = body.get(section)
            if isinstance(patch, dict):
                data[section].update(patch)
                if section == "vault" and "path" in patch:
                    vp = Path(str(patch["path"])).expanduser()
                    data["vault"]["path"] = vp
                    if not patch.get("vault_name"):
                        data["vault"]["vault_name"] = vp.name or "SesliNot"

        try:
            new_cfg = SesliNotConfig.model_validate(data)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"Geçersiz ayar: {exc}"}, status_code=400)

        new_cfg.vault.path.expanduser().mkdir(parents=True, exist_ok=True)
        write_config_toml(new_cfg, state.config_path)
        new_cfg.paths.ensure_dirs()
        VaultWriter(new_cfg).ensure_bank_scaffolding()
        state.reload_config(new_cfg)
        state.notify_subscribers()
        return JSONResponse(
            {
                "ok": True,
                "settings": _serialize_settings(new_cfg, state.config_path),
                "snapshot": state.build_snapshot(),
            }
        )

    async def complete_onboarding(request: Request) -> Response:
        if err := _check_auth(request, state):
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Geçersiz JSON"}, status_code=400)

        vault_raw = str(body.get("vaultPath") or "").strip()
        if not vault_raw:
            return JSONResponse({"ok": False, "error": "Obsidian kasa yolu zorunlu"}, status_code=400)

        vault_path = Path(vault_raw).expanduser().resolve()
        vault_path.mkdir(parents=True, exist_ok=True)

        secrets_patch = body.get("secrets")
        if isinstance(secrets_patch, dict):
            save_secrets_env(secrets_patch, state.config_path)

        stt_provider = str(body.get("sttProvider") or "groq")
        if stt_provider not in {"local", "groq", "openai", "custom"}:
            stt_provider = "local"

        research_mode = str(body.get("researchMode") or "api")  # cli | api
        api_provider = str(body.get("apiProvider") or "openai")
        if api_provider not in {"openai", "anthropic"}:
            api_provider = "openai"

        plat = current_platform()
        capture_backend = "pipewire" if (plat == "linux" and shutil.which("pw-record")) else "sounddevice"

        data = state.cfg.model_dump()
        data["vault"]["path"] = vault_path
        data["vault"]["vault_name"] = vault_path.name
        data["vault"]["hafiza_os_mode"] = "auto"
        data["audio"]["capture_backend"] = capture_backend
        data["audio"]["stt_provider"] = stt_provider
        if body.get("language"):
            data["audio"]["language"] = str(body["language"])

        if research_mode == "api":
            model = str(body.get("apiModel") or ("claude-sonnet-4-20250514" if api_provider == "anthropic" else "gpt-4o-mini"))
            data["router"]["backends"] = ["api"]
            data["research"]["lead_backend"] = "api"
            data["research"]["worker_backend"] = "api"
            data["research"]["synth_backend"] = "api"
            data["research"]["audit_backend"] = "api"
            data["research"]["lead_model"] = model
            data["research"]["worker_model"] = model
            data["research"]["synth_model"] = model
            data["research"]["audit_model"] = model
            data["research"]["api_provider"] = api_provider
            data["research"]["api_model"] = model
        else:
            ordered = [b for b in ("claude", "agy", "codex") if shutil.which(b)] or ["claude", "agy", "codex"]
            data["router"]["backends"] = ordered
            lead = "claude" if "claude" in ordered else ordered[0]
            worker = "codex" if "codex" in ordered else lead
            data["research"]["lead_backend"] = lead
            data["research"]["worker_backend"] = worker
            data["research"]["synth_backend"] = lead
            data["research"]["audit_backend"] = worker

        data["research"]["require_save_approval"] = bool(body.get("requireSaveApproval", True))
        if body.get("hotkeyToggle"):
            data["desktop"]["hotkey_toggle"] = str(body["hotkeyToggle"])

        new_cfg = SesliNotConfig.model_validate(data)
        write_config_toml(new_cfg, state.config_path)
        new_cfg.paths.ensure_dirs()
        VaultWriter(new_cfg).ensure_bank_scaffolding()
        state.reload_config(new_cfg)
        state.notify_subscribers()
        return JSONResponse(
            {
                "ok": True,
                "settings": _serialize_settings(new_cfg, state.config_path),
                "snapshot": state.build_snapshot(),
            }
        )

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/api/record/{action:str}", record_action, methods=["POST"]),
        Route("/api/notes", create_note, methods=["POST"]),
        Route("/api/research", create_research, methods=["POST"]),
        Route("/api/jobs/{job_id:str}/preview", job_preview, methods=["GET"]),
        Route("/api/jobs/{job_id:str}/{action:str}", job_action, methods=["POST"]),
        Route("/api/open", open_item, methods=["POST"]),
        Route("/api/vaults", list_vaults, methods=["GET"]),
        Route("/api/doctor", get_doctor, methods=["GET"]),
        Route("/api/settings", get_settings, methods=["GET"]),
        Route("/api/settings", update_settings, methods=["PUT"]),
        Route("/api/onboarding/complete", complete_onboarding, methods=["POST"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.seslinot = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "https://tauri.localhost",
            "http://tauri.localhost",
        ],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    return app


def run_server(
    cfg: SesliNotConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    config_path: Path | None = None,
    no_daemon: bool = False,
) -> int:
    """Loopback üzerinde HTTP+SSE sunucusunu başlatır ve hazır bilgisini stdout'a JSON yazar."""
    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Güvenlik gereği seslinot server yalnızca loopback (127.0.0.1) üzerinde çalışabilir.")

    api_token = token or os.environ.get("SESLINOT_SERVER_TOKEN") or secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    actual_port = sock.getsockname()[1]
    sock.listen(64)

    app = create_app(
        cfg,
        config_path=config_path,
        api_token=api_token,
        start_daemon=not no_daemon,
    )

    ready_line = json.dumps(
        {"event": "ready", "host": host, "port": actual_port, "token": api_token, "version": __version__},
        ensure_ascii=False,
    )
    print(ready_line, flush=True)

    uv_config = uvicorn.Config(
        app,
        host=host,
        port=actual_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    asyncio.run(server.serve(sockets=[sock]))
    return 0
