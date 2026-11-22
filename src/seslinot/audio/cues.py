"""Sesli geri bildirim tonları (pure-Python WAV) ve crash-safe PipeWire ses kısıcı (ducker)."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Literal

from seslinot.config import SesliNotConfig

logger = logging.getLogger(__name__)

CueKind = Literal["start", "stop", "cancel", "done"]

CUE_SAMPLE_RATE = 48_000
START_CUE_SETTLE_SECS = 0.08
_EXCLUDED_MEDIA_ROLES = frozenset({"event", "notification"})


def _envelope(index: int, total: int, fade_samples: int) -> float:
    """Tıklama/çıtırtı önleyen yumuşak sinüs zarfı (attack/release)."""
    if total <= 0:
        return 0.0
    fade = min(fade_samples, total // 2)
    if fade <= 0:
        return 1.0
    if index < fade:
        return 0.5 * (1.0 - math.cos(math.pi * index / fade))
    tail = total - 1 - index
    if tail < fade:
        return 0.5 * (1.0 - math.cos(math.pi * tail / fade))
    return 1.0


def _write_mono_wav(path: Path, samples: list[float], sample_rate: int = CUE_SAMPLE_RATE) -> None:
    """[-1.0, 1.0] kayan nokta örneklerini 16-bit mono PCM WAV olarak atomik yazar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = bytearray(len(samples) * 2)
    for i, val in enumerate(samples):
        clamped = max(-1.0, min(1.0, val))
        int_val = int(round(clamped * 32767.0))
        struct.pack_into("<h", pcm, i * 2, int_val)

    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(pcm))
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _synth_start_samples(sample_rate: int = CUE_SAMPLE_RATE) -> list[float]:
    """~90ms yükselen çift ton (520Hz -> 780Hz)."""
    total = int(sample_rate * 0.090)
    mid = total // 2
    fade = int(sample_rate * 0.008)
    amp = 0.32
    samples: list[float] = []
    phase = 0.0
    for i in range(total):
        freq = 520.0 if i < mid else 780.0
        phase += 2.0 * math.pi * freq / sample_rate
        env = _envelope(i, total, fade)
        samples.append(amp * env * math.sin(phase))
    return samples


def _synth_stop_samples(sample_rate: int = CUE_SAMPLE_RATE) -> list[float]:
    """~100ms alçalan yumuşak ton (720Hz -> 440Hz)."""
    total = int(sample_rate * 0.100)
    fade = int(sample_rate * 0.010)
    amp = 0.30
    samples: list[float] = []
    phase = 0.0
    for i in range(total):
        frac = i / max(1, total - 1)
        freq = 720.0 - (280.0 * frac)
        phase += 2.0 * math.pi * freq / sample_rate
        env = _envelope(i, total, fade)
        samples.append(amp * env * math.sin(phase))
    return samples


def _synth_cancel_samples(sample_rate: int = CUE_SAMPLE_RATE) -> list[float]:
    """~110ms pes çift tık (260Hz + boşluk + 210Hz)."""
    total = int(sample_rate * 0.110)
    pulse = int(sample_rate * 0.036)
    gap = int(sample_rate * 0.028)
    fade = int(sample_rate * 0.006)
    amp = 0.30
    samples = [0.0] * total

    for i in range(min(pulse, total)):
        t = i / sample_rate
        env = _envelope(i, pulse, fade)
        samples[i] = amp * env * math.sin(2.0 * math.pi * 260.0 * t)

    second_start = pulse + gap
    second_len = min(pulse, max(0, total - second_start))
    for i in range(second_len):
        t = i / sample_rate
        env = _envelope(i, second_len, fade)
        samples[second_start + i] = amp * env * math.sin(2.0 * math.pi * 210.0 * t)

    return samples


def _synth_done_samples(sample_rate: int = CUE_SAMPLE_RATE) -> list[float]:
    """~140ms sıcak yumuşak majör akor (C5 + E5 + G5)."""
    total = int(sample_rate * 0.140)
    fade = int(sample_rate * 0.012)
    amp = 0.28
    freqs = (523.25, 659.25, 783.99)
    weights = (0.45, 0.33, 0.22)
    samples: list[float] = []
    for i in range(total):
        t = i / sample_rate
        decay = math.exp(-5.0 * t)
        env = _envelope(i, total, fade) * decay
        val = sum(w * math.sin(2.0 * math.pi * f * t) for f, w in zip(freqs, weights))
        samples.append(amp * env * val)
    return samples


_CUE_GENERATORS = {
    "start": _synth_start_samples,
    "stop": _synth_stop_samples,
    "cancel": _synth_cancel_samples,
    "done": _synth_done_samples,
}


def ensure_cue_sounds(sounds_dir: Path) -> dict[CueKind, Path]:
    """Eksik sesli geri bildirim WAV dosyalarını (48kHz mono PCM) üretir."""
    sounds_dir = Path(sounds_dir)
    sounds_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[CueKind, Path] = {}
    for kind, synth_fn in _CUE_GENERATORS.items():
        cue_path = sounds_dir / f"{kind}.wav"
        if not cue_path.is_file() or cue_path.stat().st_size == 0:
            _write_mono_wav(cue_path, synth_fn(CUE_SAMPLE_RATE), CUE_SAMPLE_RATE)
        paths[kind] = cue_path  # type: ignore[index]
    return paths


def play_cue(
    kind: CueKind,
    cfg: SesliNotConfig,
    *,
    blocking: bool | None = None,
) -> bool:
    """İlgili cue sesini pw-play (yedek: paplay) ile çalar.

    `start` için varsayılan `blocking=True` olup çalma sonrası 80ms beklenir;
    böylece başlangıç tonu mikrofon kaydına sızmaz.
    """
    if not cfg.audio.cues_enabled:
        return False

    cue_paths = ensure_cue_sounds(cfg.paths.sounds_dir)
    wav_path = cue_paths[kind]
    vol = max(0.0, min(1.0, float(cfg.audio.cue_volume)))
    should_block = (kind == "start") if blocking is None else blocking

    from seslinot.platform.os_compat import current_platform, popen_isolated_kwargs

    plat = current_platform()
    if plat == "windows":
        try:
            import winsound  # type: ignore[import-not-found]

            flags = winsound.SND_FILENAME | (0 if should_block else winsound.SND_ASYNC)
            winsound.PlaySound(str(wav_path), flags)
            if should_block and kind == "start" and START_CUE_SETTLE_SECS > 0:
                time.sleep(START_CUE_SETTLE_SECS)
            return True
        except Exception as exc:
            logger.debug("Windows winsound cue oynatılamadı (%s): %s", kind, exc)
            return False

    cmd: list[str] | None = None
    if plat == "macos" and shutil.which("afplay"):
        cmd = ["afplay", "-v", f"{vol:.2f}", str(wav_path)]
    elif shutil.which("pw-play"):
        cmd = [
            "pw-play",
            "--volume",
            f"{vol:.2f}",
            "--media-category",
            "Playback",
            "--media-role",
            "Notification",
            str(wav_path),
        ]
    elif shutil.which("paplay"):
        pa_vol = int(round(vol * 65536))
        cmd = [
            "paplay",
            f"--volume={pa_vol}",
            "--property=media.role=notification",
            str(wav_path),
        ]

    if cmd is None:
        return False

    try:
        if should_block:
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
            if kind == "start" and START_CUE_SETTLE_SECS > 0:
                time.sleep(START_CUE_SETTLE_SECS)
        else:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_isolated_kwargs(),
            )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Cue oynatılamadı (%s): %s", kind, exc)
        return False


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _extract_channel_volumes(volume_obj: Any) -> list[int]:
    """pactl JSON `volume` nesnesinden kanal başına tam sayı ses değerlerini çıkarır."""
    if not isinstance(volume_obj, dict):
        return []
    values: list[int] = []
    for ch_info in volume_obj.values():
        if isinstance(ch_info, dict):
            raw_val = ch_info.get("value")
            if isinstance(raw_val, int):
                values.append(max(0, raw_val))
                continue
            pct = ch_info.get("value_percent")
            if isinstance(pct, str) and pct.strip().endswith("%"):
                try:
                    pct_num = float(pct.strip()[:-1])
                    values.append(max(0, int(round(pct_num * 65536.0 / 100.0))))
                except ValueError:
                    pass
        elif isinstance(ch_info, int):
            values.append(max(0, ch_info))
    return values


def _should_exclude_stream(stream: dict[str, Any]) -> bool:
    """Ducking dışında bırakılması gereken akışları belirler."""
    if bool(stream.get("corked", False)):
        return True

    props = stream.get("properties")
    if not isinstance(props, dict):
        props = {}

    media_role = str(props.get("media.role", "")).strip().lower()
    if media_role in _EXCLUDED_MEDIA_ROLES:
        return True

    identity_fields = (
        props.get("application.name"),
        props.get("application.id"),
        props.get("application.process.binary"),
        props.get("node.name"),
        props.get("media.name"),
    )
    for field in identity_fields:
        if isinstance(field, str) and "seslinot" in field.lower():
            return True

    return False


class PipeWireDucker:
    """Aktif medya akışlarının sesini crash-safe biçimde kısıp geri yükler."""

    def __init__(self, duck_state_path: Path, ratio: float = 0.30) -> None:
        self.duck_state_path = Path(duck_state_path)
        self.ratio = max(0.0, min(1.0, float(ratio)))

    def _load_saved_state(self) -> dict[str, list[int]]:
        if not self.duck_state_path.is_file():
            return {}
        try:
            raw = json.loads(self.duck_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        streams = raw.get("streams") if isinstance(raw, dict) else None
        if not isinstance(streams, dict):
            return {}
        parsed: dict[str, list[int]] = {}
        for sid, vols in streams.items():
            if isinstance(vols, list) and all(isinstance(v, int) for v in vols):
                parsed[str(sid)] = list(vols)
        return parsed

    def duck(self) -> int:
        """Uygun sink-input akışlarını kısar ve orijinal sesleri atomik kaydeder."""
        if not shutil.which("pactl"):
            return 0

        try:
            proc = subprocess.run(
                ["pactl", "--format=json", "list", "sink-inputs"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("pactl list sink-inputs başarısız: %s", exc)
            return 0

        if proc.returncode != 0 or not proc.stdout.strip():
            return 0

        try:
            sink_inputs = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.debug("pactl JSON ayrıştırılamadı: %s", exc)
            return 0

        if not isinstance(sink_inputs, list):
            return 0

        saved_originals = self._load_saved_state()
        to_duck: list[tuple[str, list[int]]] = []

        for stream in sink_inputs:
            if not isinstance(stream, dict):
                continue
            if _should_exclude_stream(stream):
                continue

            idx = stream.get("index")
            if idx is None:
                continue
            sid = str(idx)
            # Daha önce kısılmış akışı tekrar kısma (çifte ducking koruması)
            if sid in saved_originals:
                continue

            orig_vols = _extract_channel_volumes(stream.get("volume"))
            if not orig_vols:
                continue

            saved_originals[sid] = orig_vols
            ducked_vols = [max(0, int(round(v * self.ratio))) for v in orig_vols]
            to_duck.append((sid, ducked_vols))

        if not to_duck:
            return 0

        # Crash-safe: sesleri kısmadan ÖNCE orijinal değerleri atomik olarak diske yaz.
        _atomic_write_json(
            self.duck_state_path,
            {
                "ratio": self.ratio,
                "saved_at": int(time.time()),
                "streams": saved_originals,
            },
        )

        ducked_count = 0
        for sid, ducked_vols in to_duck:
            cmd = ["pactl", "set-sink-input-volume", sid, *[str(v) for v in ducked_vols]]
            try:
                res = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3.0,
                )
                if res.returncode == 0:
                    ducked_count += 1
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("sink-input %s kısılamadı: %s", sid, exc)

        return ducked_count

    def restore(self) -> int:
        """duck_state.json içindeki orijinal sesleri geri yükler ve durum dosyasını siler."""
        if not self.duck_state_path.is_file():
            return 0

        saved_streams = self._load_saved_state()
        restored = 0

        if saved_streams and shutil.which("pactl"):
            for sid, orig_vols in saved_streams.items():
                if not orig_vols:
                    continue
                cmd = ["pactl", "set-sink-input-volume", sid, *[str(v) for v in orig_vols]]
                try:
                    res = subprocess.run(
                        cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=3.0,
                    )
                    if res.returncode == 0:
                        restored += 1
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.debug("sink-input %s geri yüklenemedi: %s", sid, exc)

        self.duck_state_path.unlink(missing_ok=True)
        return restored
