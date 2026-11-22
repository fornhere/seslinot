"""PipeWire segmentli ses yakalama denetleyicisi, WAV birleştirici ve arşivleme politikası."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import wave
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seslinot.audio.cues import PipeWireDucker, _atomic_write_json, play_cue
from seslinot.config import SesliNotConfig
from seslinot.models import VoiceState
from seslinot.platform.os_compat import current_platform, popen_isolated_kwargs

logger = logging.getLogger(__name__)

_SAFE_NOTE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_OPUS_RETENTION_DAYS = 30
_MERGE_FRAME_BLOCK = 65_536


class CaptureError(RuntimeError):
    """Ses yakalama veya WAV birleştirme yaşam döngüsü hatası."""


def _sanitize_note_id(note_id: str) -> str:
    cleaned = _SAFE_NOTE_ID_RE.sub("_", note_id.strip()).strip("._-")
    if not cleaned:
        raise CaptureError("Geçersiz note_id: boş dosya adı üretilemez.")
    return cleaned


def _is_pid_alive(pid: int | None) -> bool:
    """Verilen PID'in canlı (ve zombie olmayan) bir süreç olup olmadığını denetler."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            try:
                WAIT_TIMEOUT = 0x00000102
                return bool(ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            stat_text = stat_path.read_text(encoding="utf-8", errors="ignore")
            rparen = stat_text.rfind(")")
            if rparen != -1:
                fields = stat_text[rparen + 2 :].split()
                if fields and fields[0] == "Z":
                    return False
        except OSError:
            pass
    return True


def merge_wav_parts(parts: Sequence[Path], output_path: Path) -> Path:
    """Birden fazla 16kHz mono PCM WAV parçasını yeniden kodlamadan tek WAV'da birleştirir."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")

    valid_parts: list[Path] = [Path(p) for p in parts if Path(p).is_file() and Path(p).stat().st_size > 44]
    if not valid_parts:
        raise CaptureError("Birleştirilecek geçerli WAV parçası bulunamadı.")

    expected_params: tuple[int, int, int] | None = None  # (nchannels, sampwidth, framerate)
    total_frames = 0

    try:
        out_wf: wave.Wave_write | None = None
        try:
            for part in valid_parts:
                try:
                    with wave.open(str(part), "rb") as in_wf:
                        params = (
                            in_wf.getnchannels(),
                            in_wf.getsampwidth(),
                            in_wf.getframerate(),
                        )
                        nframes = in_wf.getnframes()
                        if nframes <= 0:
                            continue

                        if expected_params is None:
                            expected_params = params
                            out_wf = wave.open(str(tmp_path), "wb")
                            out_wf.setnchannels(params[0])
                            out_wf.setsampwidth(params[1])
                            out_wf.setframerate(params[2])
                        elif params != expected_params:
                            raise CaptureError(
                                f"Uyumsuz WAV parametreleri ({part.name}): "
                                f"{params} != {expected_params}"
                            )

                        assert out_wf is not None
                        remaining = nframes
                        while remaining > 0:
                            chunk_frames = min(_MERGE_FRAME_BLOCK, remaining)
                            raw = in_wf.readframes(chunk_frames)
                            if not raw:
                                break
                            out_wf.writeframesraw(raw)
                            total_frames += len(raw) // (params[0] * params[1])
                            remaining -= chunk_frames
                except wave.Error as exc:
                    logger.warning("Bozuk WAV parçası atlandı (%s): %s", part, exc)
                    continue
        finally:
            if out_wf is not None:
                out_wf.close()

        if total_frames <= 0 or not tmp_path.is_file():
            raise CaptureError("Birleştirilen WAV dosyası boş; ses karesi bulunamadı.")

        os.replace(tmp_path, output_path)
        return output_path
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


class AudioCaptureController:
    """PipeWire (pw-record) tabanlı başlat/duraklat/devam/durdur/iptal kayıt denetleyicisi."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg
        self.ducker = PipeWireDucker(
            cfg.paths.duck_state_path,
            ratio=cfg.audio.duck_ratio,
        )
        self._active_proc: subprocess.Popen[bytes] | None = None

    @property
    def state_path(self) -> Path:
        return self.cfg.paths.capture_state_path

    @property
    def _stop_flag_path(self) -> Path:
        return self.cfg.paths.runtime_dir / "capture.stop"

    def _load_state(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _clear_state(self) -> None:
        self.state_path.unlink(missing_ok=True)
        self._stop_flag_path.unlink(missing_ok=True)
        self._active_proc = None

    def _pid_alive(self, pid: int | None) -> bool:
        if pid is None:
            return False
        if self._active_proc is not None and self._active_proc.pid == pid:
            return self._active_proc.poll() is None
        return _is_pid_alive(pid)

    def _resolve_backend(self) -> str:
        pref = getattr(self.cfg.audio, "capture_backend", "auto")
        if pref == "pipewire":
            return "pipewire"
        if pref == "sounddevice":
            return "sounddevice"
        if current_platform() == "linux" and shutil.which("pw-record"):
            return "pipewire"
        return "sounddevice"

    def _spawn_pw_record(self, part_path: Path) -> subprocess.Popen[bytes]:
        backend = self._resolve_backend()
        part_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_flag_path.unlink(missing_ok=True)

        if backend == "pipewire":
            if not shutil.which("pw-record"):
                raise CaptureError("pw-record komutu sistem PATH üzerinde bulunamadı.")
            cmd = [
                "pw-record",
                "--rate",
                str(self.cfg.audio.sample_rate),
                "--channels",
                str(self.cfg.audio.channels),
                "--format",
                "s16",
                "--container",
                "wav",
                "--media-type",
                "Audio",
                "--media-category",
                "Capture",
                "--media-role",
                "Communication",
                str(part_path),
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "seslinot.audio._sd_record",
                str(part_path),
                str(self.cfg.audio.sample_rate),
                str(self.cfg.audio.channels),
                str(self._stop_flag_path),
            ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_isolated_kwargs(),
            )
        except OSError as exc:
            raise CaptureError(f"Ses kayıt süreci ({backend}) başlatılamadı: {exc}") from exc

        self._active_proc = proc
        return proc

    def _stop_recorder(self, pid: int | None, timeout: float = 3.0) -> None:
        """Aktif kayıt sürecini temiz durdurup WAV başlığını kapatmasını bekler."""
        if pid is None:
            return

        try:
            self._stop_flag_path.parent.mkdir(parents=True, exist_ok=True)
            self._stop_flag_path.write_text("stop\n", encoding="utf-8")
        except OSError:
            pass

        managed_proc = (
            self._active_proc
            if self._active_proc is not None and self._active_proc.pid == pid
            else None
        )

        if managed_proc is not None:
            if managed_proc.poll() is not None:
                self._active_proc = None
                return
            if current_platform() != "windows":
                try:
                    managed_proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    self._active_proc = None
                    return
            try:
                managed_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Kayıt süreci (PID %s) temiz kapanmadı, terminate gönderiliyor.", pid)
                managed_proc.terminate()
                try:
                    managed_proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    managed_proc.kill()
                    managed_proc.wait(timeout=1.0)
            self._active_proc = None
            return

        if not _is_pid_alive(pid):
            return

        if current_platform() != "windows":
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                return

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _is_pid_alive(pid):
                return
            time.sleep(0.025)

        logger.warning("Kayıt süreci (PID %s) temiz kapanmadı, sonlandırılıyor.", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        term_deadline = time.monotonic() + 1.0
        while time.monotonic() < term_deadline:
            if not _is_pid_alive(pid):
                return
            time.sleep(0.025)

        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sigkill)
        except (ProcessLookupError, OSError):
            pass

    def start(self) -> dict[str, Any]:
        """Yeni bir sesli not kayıt oturumu başlatır."""
        existing = self._load_state()
        if existing is not None:
            ex_state = existing.get("state")
            ex_pid = existing.get("pid")
            if ex_state == VoiceState.PAUSED.value:
                raise CaptureError("Duraklatılmış aktif bir kayıt oturumu zaten var.")
            if ex_state == VoiceState.RECORDING.value and self._pid_alive(ex_pid):
                raise CaptureError("Aktif bir kayıt oturumu zaten devam ediyor.")
            # Yetim/çökmüş eski kayıt durumunu temizle
            self._clear_state()

        self.cfg.paths.ensure_dirs()
        self.ducker.restore()
        play_cue("start", self.cfg, blocking=True)

        if self.cfg.audio.duck_enabled:
            self.ducker.duck()

        now = datetime.now(timezone.utc)
        session_id = f"sn-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        session_dir = self.cfg.paths.sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        part_path = session_dir / "part_001.wav"

        try:
            proc = self._spawn_pw_record(part_path)
        except Exception:
            self.ducker.restore()
            shutil.rmtree(session_dir, ignore_errors=True)
            raise

        state: dict[str, Any] = {
            "session_id": session_id,
            "state": VoiceState.RECORDING.value,
            "pid": proc.pid,
            "parts": [str(part_path)],
            "started_at": int(time.time()),
            "session_dir": str(session_dir),
        }
        self._save_state(state)
        return state

    def pause(self) -> dict[str, Any]:
        """Aktif kaydı temiz şekilde duraklatır ve ses kısıcısını geri yükler."""
        state = self._load_state()
        if state is None or state.get("state") != VoiceState.RECORDING.value:
            raise CaptureError("Duraklatılacak aktif bir kayıt oturumu yok.")

        self._stop_recorder(state.get("pid"))
        self.ducker.restore()

        state["state"] = VoiceState.PAUSED.value
        state["pid"] = None
        self._save_state(state)
        return state

    def resume(self) -> dict[str, Any]:
        """Duraklatılmış kayıt oturumunu yeni WAV parçası ile sürdürür."""
        state = self._load_state()
        if state is None or state.get("state") != VoiceState.PAUSED.value:
            raise CaptureError("Devam ettirilecek duraklatılmış bir kayıt oturumu yok.")

        if self.cfg.audio.duck_enabled:
            self.ducker.duck()

        parts: list[str] = list(state.get("parts", []))
        session_dir = Path(state["session_dir"])
        next_part = session_dir / f"part_{len(parts) + 1:03d}.wav"

        try:
            proc = self._spawn_pw_record(next_part)
        except Exception:
            self.ducker.restore()
            raise

        parts.append(str(next_part))
        state["parts"] = parts
        state["state"] = VoiceState.RECORDING.value
        state["pid"] = proc.pid
        self._save_state(state)
        return state

    def stop(self) -> Path:
        """Kaydı sonlandırır, WAV parçalarını spool dizininde birleştirir ve yolunu döndürür."""
        state = self._load_state()
        if state is None or state.get("state") not in (
            VoiceState.RECORDING.value,
            VoiceState.PAUSED.value,
        ):
            raise CaptureError("Durdurulacak aktif bir kayıt oturumu yok.")

        session_id = str(state["session_id"])
        session_dir = Path(state["session_dir"])
        parts = [Path(p) for p in state.get("parts", [])]

        try:
            if state.get("state") == VoiceState.RECORDING.value:
                self._stop_recorder(state.get("pid"))
            self.ducker.restore()
            play_cue("stop", self.cfg, blocking=False)

            self.cfg.paths.spool_dir.mkdir(parents=True, exist_ok=True)
            spool_wav = self.cfg.paths.spool_dir / f"{session_id}.wav"
            merged = merge_wav_parts(parts, spool_wav)
        finally:
            self.ducker.restore()
            self._clear_state()

        # Birleştirme spool'da doğrulandıktan sonra geçici parça dizinini temizle
        shutil.rmtree(session_dir, ignore_errors=True)
        return merged

    def cancel(self) -> None:
        """Aktif veya duraklatılmış kaydı iptal eder ve geçici dosyaları temizler."""
        state = self._load_state()
        if state is None:
            self.ducker.restore()
            return

        pid = state.get("pid")
        session_dir_raw = state.get("session_dir")

        try:
            if pid is not None:
                self._stop_recorder(pid)
            if session_dir_raw:
                shutil.rmtree(Path(session_dir_raw), ignore_errors=True)
            self.ducker.restore()
            play_cue("cancel", self.cfg, blocking=False)
        finally:
            self._clear_state()

    def status(self) -> dict[str, Any]:
        """Anlık ses kaydı durumunu (`idle|recording|paused`) ve başlama zamanını döndürür."""
        state = self._load_state()
        if state is None:
            return {
                "state": VoiceState.IDLE.value,
                "voice_state": VoiceState.IDLE.value,
                "recording_started_at": 0,
                "session_id": None,
                "parts_count": 0,
            }

        raw_state = str(state.get("state", VoiceState.IDLE.value))
        pid = state.get("pid")
        if raw_state == VoiceState.RECORDING.value and not self._pid_alive(pid):
            return {
                "state": VoiceState.IDLE.value,
                "voice_state": VoiceState.IDLE.value,
                "recording_started_at": 0,
                "session_id": None,
                "parts_count": 0,
            }

        started_at = int(state.get("started_at", 0) or 0)
        return {
            "state": raw_state,
            "voice_state": raw_state,
            "recording_started_at": started_at,
            "session_id": state.get("session_id"),
            "parts_count": len(state.get("parts", [])),
        }


def _purge_expired_opus(audio_dir: Path, *, max_age_days: int = _OPUS_RETENTION_DAYS, keep: Path | None = None) -> int:
    """Belirtilen günden eski .opus arşiv dosyalarını temizler."""
    if not audio_dir.is_dir():
        return 0
    cutoff = time.time() - (max_age_days * 86_400)
    keep_resolved = keep.resolve() if keep is not None and keep.exists() else None
    deleted = 0
    for opus_file in audio_dir.rglob("*.opus"):
        try:
            if keep_resolved is not None and opus_file.resolve() == keep_resolved:
                continue
            if opus_file.stat().st_mtime < cutoff:
                opus_file.unlink(missing_ok=True)
                deleted += 1
        except OSError:
            continue
    return deleted


def archive_or_cleanup_audio(
    wav_path: Path,
    note_id: str,
    cfg: SesliNotConfig,
) -> Path | None:
    """Yapılandırılan `retention` politikasına göre spool WAV dosyasını arşivler veya siler.

    - `delete_after_verify`: Spool WAV dosyasını siler ve `None` döndürür.
    - `opus` / `opus_30d`: `ffmpeg` ile 24kbps VoIP Opus'a dönüştürür, başarı
      doğrulanınca spool WAV'ı siler. `opus_30d` ayrıca 30 günden eski `.opus`
      dosyalarını temizler. Dönüşüm başarısızsa spool WAV korunur (fail-closed).
    """
    wav_path = Path(wav_path)
    policy = (cfg.audio.retention or "").strip().lower()

    if policy == "delete_after_verify":
        wav_path.unlink(missing_ok=True)
        return None

    if policy not in ("opus", "opus_30d"):
        logger.warning("Bilinmeyen retention politikası (%s); spool dosyası korunuyor.", policy)
        return None

    if not wav_path.is_file():
        return None

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg bulunamadı; spool WAV korunuyor: %s", wav_path)
        return None

    safe_id = _sanitize_note_id(note_id)
    now = datetime.now(timezone.utc)
    target_path = cfg.paths.audio_dir / f"{now:%Y}" / f"{now:%m}" / f"{safe_id}.opus"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-c:a",
        "libopus",
        "-b:a",
        "24k",
        "-vbr",
        "on",
        "-application",
        "voip",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(target_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=120.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg opus arşivleme hatası: %s", exc)
        target_path.unlink(missing_ok=True)
        return None

    if proc.returncode != 0 or not target_path.is_file() or target_path.stat().st_size == 0:
        logger.warning(
            "ffmpeg opus arşivleme başarısız (rc=%s): %s",
            proc.returncode,
            (proc.stderr or "").strip()[:300],
        )
        target_path.unlink(missing_ok=True)
        return None

    wav_path.unlink(missing_ok=True)

    if policy == "opus_30d":
        _purge_expired_opus(cfg.paths.audio_dir, max_age_days=_OPUS_RETENTION_DAYS, keep=target_path)

    return target_path
