"""SesliNot arka plan daemon'ı (seslinotd): ağ izleme, yönlendirme, araştırma dağıtımı ve kontrol."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import re
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from seslinot.config import SesliNotConfig, load_config
from seslinot.db import SesliNotDB
from seslinot.desktop.notify import OmarchyNotifier, is_valid_obsidian_uri, make_obsidian_uri
from seslinot.desktop.osd import OmarchyOSD
from seslinot.models import (
    PHASE_LABELS_TR,
    DesktopState,
    JobStatus,
    ResearchJob,
    ResearchPhase,
    RouteDecision,
    VoiceState,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

ROUTE_BACKOFF_SECS: tuple[int, ...] = (30, 120, 600)
ROUTE_MAX_ATTEMPTS = 5
OFFLINE_PROMOTE_MAX_AGE_HOURS = 24.0

_SAFE_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}$")

_CANCELLABLE_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.PENDING_ROUTE,
        JobStatus.PENDING_CONFIRM,
        JobStatus.QUEUED_OFFLINE,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
    }
)


@dataclass
class ResearchExecutionResult:
    """Bir araştırma işinin tamamlandığında daemon'a döndürdüğü özet sonuç."""

    report_rel_path: str = ""
    obsidian_uri: str = ""
    source_count: int = 0
    session_dir: str = ""
    unit_name: str = ""


@dataclass
class DaemonTickSummary:
    """Tek bir `run_once()` döngüsünün gözlemlenebilir özeti."""

    online: bool = True
    promoted_offline: int = 0
    staled_offline: int = 0
    routed_jobs: int = 0
    dispatched_jobs: int = 0
    state_written: bool = False


PhaseCallback = Callable[[ResearchPhase, str], Awaitable[None] | None]


class RouteHandler(Protocol):
    """PENDING_ROUTE işlerini değerlendiren enjekte edilebilir yönlendirici sözleşmesi."""

    def __call__(self, job: ResearchJob) -> RouteDecision | Awaitable[RouteDecision]: ...


class ResearchHandler(Protocol):
    """QUEUED->RUNNING işlerini yürüten enjekte edilebilir araştırma sözleşmesi."""

    def __call__(
        self,
        job: ResearchJob,
        on_phase: PhaseCallback,
    ) -> ResearchExecutionResult | dict[str, Any] | None | Awaitable[ResearchExecutionResult | dict[str, Any] | None]: ...


NetworkChecker = Callable[[], bool | Awaitable[bool]]


class NetworkMonitor:
    """NetworkManager üzerinden internet bağlantı durumunu sorgular.

    Önce `nmcli`, olmazsa `busctl` ile NetworkManager Connectivity özelliğini
    okur (4 = FULL çevrimiçi). NetworkManager yoksa güvenli varsayılan olarak
    `True` döndürür.
    """

    def __init__(self, timeout_secs: float = 3.0) -> None:
        self.timeout_secs = timeout_secs

    def is_online(self) -> bool:
        nmcli_result = self._check_nmcli()
        if nmcli_result is not None:
            return nmcli_result

        busctl_result = self._check_busctl()
        if busctl_result is not None:
            return busctl_result

        return True

    async def check_online(self) -> bool:
        return await asyncio.to_thread(self.is_online)

    def _check_nmcli(self) -> bool | None:
        binary = shutil.which("nmcli")
        if not binary:
            return None
        try:
            proc = subprocess.run(
                [binary, "-t", "-f", "CONNECTIVITY", "general"],
                capture_output=True,
                text=True,
                timeout=self.timeout_secs,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if proc.returncode != 0:
            return None

        value = proc.stdout.strip().lower()
        if value == "full":
            return True
        if value in {"none", "portal", "limited"}:
            return False
        return None

    def _check_busctl(self) -> bool | None:
        binary = shutil.which("busctl")
        if not binary:
            return None
        try:
            proc = subprocess.run(
                [
                    binary,
                    "get-property",
                    "org.freedesktop.NetworkManager",
                    "/org/freedesktop/NetworkManager",
                    "org.freedesktop.NetworkManager",
                    "Connectivity",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_secs,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if proc.returncode != 0:
            return None

        tokens = proc.stdout.strip().split()
        if len(tokens) < 2:
            return None
        try:
            code = int(tokens[-1])
        except ValueError:
            return None

        if code == 4:
            return True
        if code in {1, 2, 3}:
            return False
        return None


def _future_iso(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.replace(microsecond=0).isoformat()


def _normalize_execution_result(
    raw: Any,
    *,
    vault_name: str,
) -> ResearchExecutionResult:
    if raw is None:
        return ResearchExecutionResult()

    if isinstance(raw, ResearchExecutionResult):
        res = ResearchExecutionResult(
            report_rel_path=raw.report_rel_path,
            obsidian_uri=raw.obsidian_uri,
            source_count=raw.source_count,
            session_dir=raw.session_dir,
            unit_name=raw.unit_name,
        )
    elif isinstance(raw, dict):
        res = ResearchExecutionResult(
            report_rel_path=str(raw.get("report_rel_path", "") or ""),
            obsidian_uri=str(raw.get("obsidian_uri", "") or ""),
            source_count=int(raw.get("source_count", 0) or 0),
            session_dir=str(raw.get("session_dir", "") or ""),
            unit_name=str(raw.get("unit_name", "") or ""),
        )
    else:
        res = ResearchExecutionResult(
            report_rel_path=str(getattr(raw, "report_rel_path", "") or ""),
            obsidian_uri=str(getattr(raw, "obsidian_uri", "") or ""),
            source_count=int(getattr(raw, "source_count", 0) or 0),
            session_dir=str(getattr(raw, "session_dir", "") or ""),
            unit_name=str(getattr(raw, "unit_name", "") or ""),
        )

    if not res.obsidian_uri and res.report_rel_path:
        try:
            res.obsidian_uri = make_obsidian_uri(vault_name, res.report_rel_path)
        except ValueError:
            res.obsidian_uri = ""
    elif res.obsidian_uri and not is_valid_obsidian_uri(res.obsidian_uri):
        res.obsidian_uri = ""

    return res


def _stop_systemd_unit(unit_name: str) -> bool:
    """Geçerli bir systemd kullanıcı birimini güvenli şekilde durdurur."""
    clean = unit_name.strip()
    if not clean or not _SAFE_UNIT_NAME_RE.match(clean):
        logger.warning("Geçersiz systemd birim adı reddedildi: %r", unit_name)
        return False

    systemctl = shutil.which("systemctl")
    if not systemctl:
        logger.debug("systemctl bulunamadı; birim durdurma atlandı: %s", clean)
        return False

    try:
        proc = subprocess.run(
            [systemctl, "--user", "stop", clean],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("systemd birimi durdurulamadı (%s): %s", clean, exc)
        return False


class SesliNotDaemon:
    """SesliNot arka plan işçisi ve kontrol arayüzü."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        db: SesliNotDB | None = None,
        osd: OmarchyOSD | None = None,
        notifier: OmarchyNotifier | None = None,
        route_handler: RouteHandler | None = None,
        research_handler: ResearchHandler | None = None,
        network_checker: NetworkChecker | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db or SesliNotDB(cfg.paths.db_path)
        self.osd = osd or OmarchyOSD(cfg)
        self.notifier = notifier or OmarchyNotifier(cfg)
        self._route_handler = route_handler
        self._research_handler = research_handler
        self._network_monitor = NetworkMonitor()
        self._network_checker = network_checker

        self._last_online: bool | None = None
        self._last_state_json: str | None = None
        self._route_attempts: dict[str, int] = {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event: asyncio.Event | None = None

    # --- Ağ kontrolü ---

    async def _check_online(self) -> bool:
        if self._network_checker is None:
            return await self._network_monitor.check_online()

        result = self._network_checker()
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    def _check_online_sync(self) -> bool:
        if self._network_checker is None:
            return self._network_monitor.is_online()

        result = self._network_checker()
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return bool(asyncio.run(self._await_bool(result)))
            # Aktif event loop içinde senkron çağrı: sızıntıyı önle, son bilinen duruma dön.
            if inspect.iscoroutine(result):
                result.close()
            if self._last_online is not None:
                return self._last_online
            return self._network_monitor.is_online()
        return bool(result)

    @staticmethod
    async def _await_bool(awaitable: Awaitable[bool]) -> bool:
        return bool(await awaitable)

    # --- Tembel varsayılan modül yükleyicileri ---

    def _resolve_route_handler(self) -> RouteHandler:
        if self._route_handler is not None:
            return self._route_handler

        async def _lazy_route(job: ResearchJob) -> RouteDecision:
            try:
                router_mod = importlib.import_module("seslinot.router")
            except ImportError as exc:
                raise RuntimeError(
                    "seslinot.router modülü yüklü değil ve route_handler enjekte edilmedi"
                ) from exc

            if hasattr(router_mod, "IntentRouter"):
                router = router_mod.IntentRouter(self.cfg)
                res = router.route(job.raw_prompt)
            elif hasattr(router_mod, "route_transcript"):
                res = router_mod.route_transcript(job.raw_prompt, self.cfg)
            else:
                raise RuntimeError(
                    "seslinot.router içinde IntentRouter veya route_transcript bulunamadı"
                )
            if inspect.isawaitable(res):
                res = await res
            if not isinstance(res, RouteDecision):
                return RouteDecision.model_validate(res)
            return res

        return _lazy_route

    def _resolve_research_handler(self) -> ResearchHandler:
        if self._research_handler is not None:
            return self._research_handler

        async def _lazy_research(
            job: ResearchJob,
            on_phase: PhaseCallback,
        ) -> ResearchExecutionResult | dict[str, Any] | None:
            try:
                orch_mod = importlib.import_module("seslinot.research.orchestrator")
            except ImportError:
                try:
                    orch_mod = importlib.import_module("seslinot.research")
                except ImportError as exc:
                    raise RuntimeError(
                        "seslinot.research modülü yüklü değil ve research_handler enjekte edilmedi"
                    ) from exc

            if hasattr(orch_mod, "ResearchOrchestrator"):
                orchestrator = orch_mod.ResearchOrchestrator(self.cfg, self.db)
                if hasattr(orchestrator, "run_job"):
                    res = orchestrator.run_job(job, on_phase=on_phase)
                else:
                    res = orchestrator.run(job, on_phase=on_phase)
            elif hasattr(orch_mod, "run_research_job"):
                res = orch_mod.run_research_job(self.cfg, self.db, job, on_phase=on_phase)
            else:
                raise RuntimeError(
                    "seslinot.research içinde ResearchOrchestrator veya run_research_job bulunamadı"
                )

            if inspect.isawaitable(res):
                return await res
            return res

        return _lazy_research

    # --- Durum dosyası eşitleme ---

    def _read_capture_state(self) -> tuple[VoiceState, int]:
        path = self.cfg.paths.capture_state_path
        if not path.is_file():
            return VoiceState.IDLE, 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state_str = str(raw.get("voiceState", VoiceState.IDLE.value))
            voice_state = VoiceState(state_str)
            started_at = max(0, int(raw.get("recordingStartedAt", 0) or 0))
            return voice_state, started_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return VoiceState.IDLE, 0

    def build_desktop_state(self) -> DesktopState:
        voice_state, recording_started_at = self._read_capture_state()
        return self.db.build_desktop_state(
            voice_state=voice_state,
            recording_started_at=recording_started_at,
        )

    def sync_desktop_state(self, *, force: bool = False) -> bool:
        """state.json yalnızca değiştiğinde (veya force=True iken) atomik yazılır."""
        state = self.build_desktop_state()
        serialized = state.model_dump_json()
        if not force and serialized == self._last_state_json:
            return False

        self.osd.write_desktop_state(state)
        self._last_state_json = serialized
        return True

    # --- Ana döngü adımları ---

    async def run_once(self) -> DaemonTickSummary:
        """Tek bir belirlenimci (deterministic) daemon turu çalıştırır."""
        summary = DaemonTickSummary()
        online = await self._check_online()
        summary.online = online

        previous_online = self._last_online
        self._last_online = online

        # 1. Çevrimdışı -> çevrimiçi geçişinde (veya başlangıçta çevrimdışı bekleyen varsa) yükselt
        should_promote = online and (
            previous_online is False
            or (
                previous_online is None
                and bool(self.db.list_jobs([JobStatus.QUEUED_OFFLINE], limit=1))
            )
        )
        if should_promote:
            promoted, staled = self.db.promote_offline_jobs(
                max_age_hours=OFFLINE_PROMOTE_MAX_AGE_HOURS
            )
            summary.promoted_offline = promoted
            summary.staled_offline = staled
            if staled > 0:
                self._notify_unannounced_confirm_jobs()

        # 2. Çevrimiçiyken PENDING_ROUTE işlerini yönlendir
        if online:
            summary.routed_jobs = await self._process_pending_routes()

            # 3. QUEUED işlerini atomik olarak sahiplen ve arka plan görevi başlat
            summary.dispatched_jobs = self._dispatch_queued_jobs()

        # 4. Değişiklik varsa state.json güncelle ve bar eklentisini yenile
        summary.state_written = self.sync_desktop_state()
        return summary

    def _notify_unannounced_confirm_jobs(self) -> None:
        for job in self.db.list_jobs([JobStatus.PENDING_CONFIRM], limit=20):
            if job.notif_id == 0:
                notif_id = self.notifier.notify_research_confirm(
                    job.job_id, job.topic, job.confidence
                )
                if notif_id > 0:
                    self.db.update_job_status(
                        job.job_id,
                        JobStatus.PENDING_CONFIRM,
                        notif_id=notif_id,
                    )

    async def _process_pending_routes(self) -> int:
        now_iso = utc_now_iso()
        pending_jobs = self.db.list_jobs([JobStatus.PENDING_ROUTE], limit=50)
        if not pending_jobs:
            return 0

        handler = self._resolve_route_handler()
        processed = 0
        # En eski işten başlayarak işle
        for job in reversed(pending_jobs):
            if job.next_retry_at and job.next_retry_at > now_iso:
                continue

            processed += 1
            try:
                raw_decision = handler(job)
                decision = (
                    await raw_decision
                    if inspect.isawaitable(raw_decision)
                    else raw_decision
                )
                if not isinstance(decision, RouteDecision):
                    decision = RouteDecision.model_validate(decision)
            except Exception as exc:
                self._handle_route_failure(job, exc)
                continue

            self._route_attempts.pop(job.job_id, None)
            self._apply_route_decision(job, decision)

        return processed

    def _apply_route_decision(self, job: ResearchJob, decision: RouteDecision) -> None:
        topic = decision.research_topic.strip() or job.topic
        self.db.record_event(
            job.job_id,
            "routed",
            decision.reason or f"intent={decision.intent_type}, confidence={decision.confidence:.2f}",
            payload=decision.model_dump(),
        )

        if decision.is_research and decision.confidence >= self.cfg.router.auto_start_threshold:
            self.db.update_job_status(
                job.job_id,
                JobStatus.QUEUED,
                phase=ResearchPhase.QUEUED,
                phase_detail=PHASE_LABELS_TR[ResearchPhase.QUEUED],
                error_message="",
                next_retry_at="",
            )
        elif decision.is_research and decision.confidence >= self.cfg.router.confirm_threshold:
            notif_id = self.notifier.notify_research_confirm(
                job.job_id, topic, decision.confidence
            )
            self.db.update_job_status(
                job.job_id,
                JobStatus.PENDING_CONFIRM,
                phase=ResearchPhase.QUEUED,
                phase_detail=decision.reason or "Kullanıcı onayı bekleniyor",
                notif_id=notif_id,
                error_message="",
                next_retry_at="",
            )
        else:
            self.db.update_job_status(
                job.job_id,
                JobStatus.DISMISSED,
                phase=ResearchPhase.CANCELLED,
                phase_detail=decision.reason or "Araştırma niyeti saptanmadı",
                error_message="",
                next_retry_at="",
                finished=True,
            )

    def _handle_route_failure(self, job: ResearchJob, exc: Exception) -> None:
        attempts = self._route_attempts.get(job.job_id, job.attempt) + 1
        self._route_attempts[job.job_id] = attempts
        err_msg = str(exc) or exc.__class__.__name__

        if attempts >= ROUTE_MAX_ATTEMPTS:
            self._route_attempts.pop(job.job_id, None)
            self.db.update_job_status(
                job.job_id,
                JobStatus.FAILED,
                phase=ResearchPhase.FAILED,
                phase_detail=PHASE_LABELS_TR[ResearchPhase.FAILED],
                error_message=f"Yönlendirme {attempts} denemede başarısız: {err_msg}",
                finished=True,
            )
            self.db.record_event(
                job.job_id,
                "route_failed",
                err_msg,
                payload={"attempt": attempts},
            )
            self.notifier.notify_research_failed(job.notif_id, job.topic, err_msg)
            return

        backoff_secs = ROUTE_BACKOFF_SECS[min(attempts - 1, len(ROUTE_BACKOFF_SECS) - 1)]
        retry_at = _future_iso(backoff_secs)
        self.db.update_job_status(
            job.job_id,
            JobStatus.PENDING_ROUTE,
            error_message=err_msg,
            next_retry_at=retry_at,
        )
        self.db.record_event(
            job.job_id,
            "route_retry_scheduled",
            f"Deneme {attempts}/{ROUTE_MAX_ATTEMPTS} başarısız; {backoff_secs} sn sonra yeniden denenecek",
            payload={"attempt": attempts, "backoff_secs": backoff_secs, "next_retry_at": retry_at},
        )

    def _dispatch_queued_jobs(self) -> int:
        dispatched = 0
        max_concurrent = max(1, int(self.cfg.research.max_concurrent_jobs))
        while True:
            job = self.db.claim_next_job(max_concurrent=max_concurrent)
            if job is None:
                break
            self._spawn_research_task(job)
            dispatched += 1
        return dispatched

    def _spawn_research_task(self, job: ResearchJob) -> asyncio.Task[None]:
        notif_id = self.notifier.notify_research_started(job.job_id, job.topic)
        if notif_id > 0:
            self.db.update_job_status(
                job.job_id,
                JobStatus.RUNNING,
                notif_id=notif_id,
            )
        self.db.record_event(
            job.job_id,
            ResearchPhase.VAULT_SCAN.value,
            PHASE_LABELS_TR[ResearchPhase.VAULT_SCAN],
        )
        self.sync_desktop_state(force=True)

        task = asyncio.create_task(
            self._execute_research_job(job, notif_id),
            name=f"seslinot-research-{job.job_id}",
        )
        self._running_tasks[job.job_id] = task
        return task

    async def _execute_research_job(self, job: ResearchJob, notif_id: int) -> None:
        handler = self._resolve_research_handler()
        current_notif_id = notif_id

        async def _on_phase(phase: ResearchPhase, detail: str = "") -> None:
            nonlocal current_notif_id
            latest = self.db.get_job(job.job_id)
            if latest is None or latest.status == JobStatus.CANCELLED:
                return
            label = detail or PHASE_LABELS_TR.get(phase, phase.value)
            self.db.update_job_phase(job.job_id, phase, label)
            updated_id = self.notifier.notify_research_phase(
                current_notif_id,
                job.job_id,
                job.topic,
                label,
            )
            if updated_id > 0 and updated_id != current_notif_id:
                current_notif_id = updated_id
                self.db.update_job_status(
                    job.job_id,
                    JobStatus.RUNNING,
                    notif_id=current_notif_id,
                )
            self.sync_desktop_state(force=True)

        try:
            raw_result = handler(job, _on_phase)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result

            latest = self.db.get_job(job.job_id)
            if latest is None or latest.status == JobStatus.CANCELLED:
                return

            result = _normalize_execution_result(
                raw_result, vault_name=self.cfg.vault.vault_name
            )
            self.db.update_job_status(
                job.job_id,
                JobStatus.SUCCEEDED,
                phase=ResearchPhase.COMPLETED,
                phase_detail=PHASE_LABELS_TR[ResearchPhase.COMPLETED],
                report_rel_path=result.report_rel_path,
                obsidian_uri=result.obsidian_uri,
                source_count=result.source_count,
                session_dir=result.session_dir or job.session_dir,
                unit_name=result.unit_name or job.unit_name,
                error_message="",
                finished=True,
            )
            if result.report_rel_path and job.note_id:
                self.db.update_note_research_link(job.note_id, result.report_rel_path)

            self.db.record_event(
                job.job_id,
                ResearchPhase.COMPLETED.value,
                PHASE_LABELS_TR[ResearchPhase.COMPLETED],
                payload={
                    "report_rel_path": result.report_rel_path,
                    "obsidian_uri": result.obsidian_uri,
                    "source_count": result.source_count,
                },
            )
            self.notifier.notify_research_completed(
                current_notif_id,
                job.topic,
                result.obsidian_uri,
                result.source_count,
            )
            self.sync_desktop_state(force=True)
        except asyncio.CancelledError:
            latest = self.db.get_job(job.job_id)
            if latest is not None and latest.status != JobStatus.CANCELLED:
                self.db.update_job_status(
                    job.job_id,
                    JobStatus.CANCELLED,
                    phase=ResearchPhase.CANCELLED,
                    phase_detail=PHASE_LABELS_TR[ResearchPhase.CANCELLED],
                    finished=True,
                )
                self.notifier.notify_research_cancelled(current_notif_id, job.topic)
                self.sync_desktop_state(force=True)
            raise
        except Exception as exc:
            latest = self.db.get_job(job.job_id)
            if latest is not None and latest.status == JobStatus.CANCELLED:
                return
            err_msg = str(exc) or exc.__class__.__name__
            logger.exception("Araştırma işi başarısız oldu (%s)", job.job_id)
            self.db.update_job_status(
                job.job_id,
                JobStatus.FAILED,
                phase=ResearchPhase.FAILED,
                phase_detail=PHASE_LABELS_TR[ResearchPhase.FAILED],
                error_message=err_msg,
                finished=True,
            )
            self.db.record_event(
                job.job_id,
                ResearchPhase.FAILED.value,
                err_msg,
            )
            self.notifier.notify_research_failed(current_notif_id, job.topic, err_msg)
            self.sync_desktop_state(force=True)
        finally:
            self._running_tasks.pop(job.job_id, None)

    async def wait_for_active_tasks(self, timeout: float | None = None) -> None:
        """Aktif tüm arka plan araştırma görevlerinin tamamlanmasını bekler."""
        tasks = list(self._running_tasks.values())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if pending:
            raise asyncio.TimeoutError(
                f"{len(pending)} araştırma görevi zaman aşımında tamamlanmadı"
            )

    # --- seslinot-ctl kontrol yöntemleri ---

    def confirm_job(self, job_id: str) -> bool:
        """PENDING_CONFIRM durumundaki işi onaylayarak kuyruğa alır."""
        job = self.db.get_job(job_id)
        if job is None or job.status != JobStatus.PENDING_CONFIRM:
            return False

        online = self._check_online_sync()
        target_status = JobStatus.QUEUED if online else JobStatus.QUEUED_OFFLINE
        detail = (
            PHASE_LABELS_TR[ResearchPhase.QUEUED]
            if online
            else "Çevrimdışı kuyrukta bekliyor"
        )
        self.db.update_job_status(
            job_id,
            target_status,
            phase=ResearchPhase.QUEUED,
            phase_detail=detail,
            error_message="",
            next_retry_at="",
        )
        self.db.record_event(
            job_id,
            "confirmed",
            f"Kullanıcı onayı alındı -> {target_status.value}",
        )
        self.sync_desktop_state(force=True)
        return True

    def dismiss_job(self, job_id: str) -> bool:
        """PENDING_CONFIRM durumundaki işi reddederek sadece not olarak bırakır."""
        job = self.db.get_job(job_id)
        if job is None or job.status != JobStatus.PENDING_CONFIRM:
            return False

        self.db.update_job_status(
            job_id,
            JobStatus.DISMISSED,
            phase=ResearchPhase.CANCELLED,
            phase_detail="Kullanıcı araştırmayı reddetti",
            finished=True,
        )
        self.db.record_event(job_id, "dismissed", "Kullanıcı tarafından reddedildi")
        self.sync_desktop_state(force=True)
        return True

    def cancel_job(self, job_id: str) -> bool:
        """Aktif veya kuyruktaki işi iptal eder, systemd birimini ve görevi durdurur."""
        job = self.db.get_job(job_id)
        if job is None or job.status not in _CANCELLABLE_STATUSES:
            return False

        if job.unit_name:
            _stop_systemd_unit(job.unit_name)

        self.db.update_job_status(
            job_id,
            JobStatus.CANCELLED,
            phase=ResearchPhase.CANCELLED,
            phase_detail=PHASE_LABELS_TR[ResearchPhase.CANCELLED],
            finished=True,
        )
        self.db.record_event(
            job_id,
            ResearchPhase.CANCELLED.value,
            PHASE_LABELS_TR[ResearchPhase.CANCELLED],
        )

        task = self._running_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()

        self.notifier.notify_research_cancelled(job.notif_id, job.topic)
        self.sync_desktop_state(force=True)
        return True

    # --- Uzun ömürlü servis döngüsü ---

    def request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def run_forever(self) -> None:
        """SIGINT/SIGTERM ile temiz kapanan ana daemon döngüsü."""
        self.cfg.paths.ensure_dirs()
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        registered_signals: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                registered_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass

        poll_interval = max(0.2, float(self.cfg.desktop.poll_interval_secs))
        self.sync_desktop_state(force=True)
        logger.info("seslinotd başlatıldı (poll=%.2fs)", poll_interval)

        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("seslinotd döngü adımında beklenmeyen hata")

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            for sig in registered_signals:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass

            active_tasks = list(self._running_tasks.values())
            for task in active_tasks:
                if not task.done():
                    task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)

            self.sync_desktop_state(force=True)
            logger.info("seslinotd temiz şekilde durduruldu")


def main() -> None:
    """`seslinotd` komut satırı giriş noktası."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    cfg.paths.ensure_dirs()
    daemon = SesliNotDaemon(cfg)
    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        pass
