"""Ses Etkinliği Algılama (VAD) ve Whisper <30sn akıllı parçalama katmanı."""

from __future__ import annotations

import array
import logging
import math
import operator
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from seslinot.config import SesliNotConfig

logger = logging.getLogger(__name__)

_MIN_TOTAL_SPEECH_SECS = 0.30
_FRAME_MS = 20
_LONG_SPLIT_OVERLAP_SECS = 0.40
_LONG_SPLIT_SEARCH_START_SECS = 18.0
_HARD_WHISPER_CAP_SECS = 29.5
_MAX_MERGE_GAP_SECS = 4.0


@dataclass(frozen=True)
class SpeechSegment:
    """Bir konuşma parçasının zaman aralığı ve ortalama enerji seviyesi."""

    start_sec: float
    end_sec: float
    rms_dbfs: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


def _rms_to_dbfs(rms: float) -> float:
    norm = rms / 32768.0
    if norm <= 1e-5:
        return -100.0
    return max(-100.0, 20.0 * math.log10(norm))


def _slice_rms_dbfs(samples: array.array, start: int, end: int) -> float:
    length = end - start
    if length <= 0:
        return -100.0
    chunk = samples[start:end]
    sq_sum = sum(map(operator.mul, chunk, chunk))
    return _rms_to_dbfs(math.sqrt(sq_sum / length))


def _read_mono_s16_wav(wav_path: Path) -> tuple[array.array, int]:
    """WAV dosyasını mono 16-bit işaretli PCM dizisi ve örnekleme hızı olarak okur."""
    with wave.open(str(wav_path), "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        if sampwidth != 2:
            raise ValueError(f"Desteklenmeyen örnekleme genişliği: {sampwidth * 8}-bit (16-bit bekleniyor)")
        raw = wf.readframes(nframes)

    pcm = array.array("h")
    pcm.frombytes(raw)
    if sys.byteorder == "big":
        pcm.byteswap()

    if nchannels == 1:
        return pcm, sample_rate

    # Çok kanallı WAV gelirse güvenli mono indirgeme
    mono = array.array("h", (0,)) * (len(pcm) // nchannels)
    for i in range(len(mono)):
        base = i * nchannels
        mono[i] = sum(pcm[base : base + nchannels]) // nchannels
    return mono, sample_rate


def _write_pcm_chunk(path: Path, samples: array.array, sample_rate: int) -> None:
    out_arr = array.array("h", samples)
    if sys.byteorder == "big":
        out_arr.byteswap()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(out_arr.tobytes())


def _find_silero_onnx_model(cfg: SesliNotConfig) -> Path | None:
    """Yerel Silero VAD .onnx modelini arar (.bin / ggml dosyalarını bilinçli yoksayar)."""
    candidates = [
        cfg.paths.data_dir / "models" / "silero_vad.onnx",
        Path.home() / ".local/share/seslinot/models/silero_vad.onnx",
    ]
    for path in candidates:
        if path.is_file() and path.suffix.lower() == ".onnx" and path.stat().st_size > 0:
            return path
    return None


def _detect_speech_silero_onnx(
    samples: array.array,
    sample_rate: int,
    model_path: Path,
    cfg: SesliNotConfig,
) -> list[tuple[int, int]] | None:
    """Opsiyonel onnxruntime + numpy ile Silero VAD çalıştırır; kullanılamazsa None döner."""
    if sample_rate != 16_000:
        return None
    try:
        import numpy as np  # type: ignore[import-not-found]
        import onnxruntime as ort  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        sess_opts = ort.SessionOptions()
        sess_opts.inter_op_num_threads = 1
        sess_opts.intra_op_num_threads = 1
        session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        input_names = {inp.name for inp in session.get_inputs()}

        window_size = 512  # 32ms @ 16kHz
        total = len(samples)
        if total < window_size:
            return []

        audio_f32 = np.asarray(samples, dtype=np.float32) / 32768.0
        probs: list[float] = []

        if "state" in input_names:
            # Silero VAD v5 arayüzü
            state = np.zeros((2, 1, 128), dtype=np.float32)
            sr = np.array(sample_rate, dtype=np.int64)
            for offset in range(0, total - window_size + 1, window_size):
                frame = audio_f32[offset : offset + window_size][np.newaxis, :]
                out, state = session.run(None, {"input": frame, "state": state, "sr": sr})
                probs.append(float(np.asarray(out).reshape(-1)[0]))
        elif {"h", "c"}.issubset(input_names):
            # Silero VAD v4 arayüzü
            h = np.zeros((2, 1, 64), dtype=np.float32)
            c = np.zeros((2, 1, 64), dtype=np.float32)
            sr = np.array([sample_rate], dtype=np.int64)
            for offset in range(0, total - window_size + 1, window_size):
                frame = audio_f32[offset : offset + window_size][np.newaxis, :]
                out, h, c = session.run(None, {"input": frame, "h": h, "c": c, "sr": sr})
                probs.append(float(np.asarray(out).reshape(-1)[0]))
        else:
            return None

        return _hysteresis_spans(
            values=probs,
            frame_samples=window_size,
            total_samples=total,
            sample_rate=sample_rate,
            onset_threshold=0.50,
            offset_threshold=0.35,
            min_speech_ms=cfg.audio.vad_min_speech_ms,
            min_silence_ms=cfg.audio.vad_min_silence_ms,
        )
    except Exception as exc:
        logger.debug("Silero ONNX VAD çalıştırılamadı, RMS VAD'a düşülüyor: %s", exc)
        return None


def _hysteresis_spans(
    *,
    values: list[float],
    frame_samples: int,
    total_samples: int,
    sample_rate: int,
    onset_threshold: float,
    offset_threshold: float,
    min_speech_ms: int,
    min_silence_ms: int,
) -> list[tuple[int, int]]:
    """Sabit eşikli histerezis durum makinesi ile ham konuşma aralıklarını çıkarır."""
    if not values:
        return []

    frame_ms = (frame_samples * 1000.0) / sample_rate
    min_silence_frames = max(1, int(math.ceil(min_silence_ms / frame_ms)))
    min_speech_samples = int((min_speech_ms / 1000.0) * sample_rate)

    raw_spans: list[tuple[int, int]] = []
    in_speech = False
    start_idx = 0
    last_active_idx = 0
    silence_frames = 0

    for idx, val in enumerate(values):
        if not in_speech:
            if val >= onset_threshold:
                in_speech = True
                start_idx = idx
                last_active_idx = idx
                silence_frames = 0
        else:
            if val >= offset_threshold:
                last_active_idx = idx
                silence_frames = 0
            else:
                silence_frames += 1
                if silence_frames >= min_silence_frames:
                    s_sample = start_idx * frame_samples
                    e_sample = min(total_samples, (last_active_idx + 1) * frame_samples)
                    if e_sample - s_sample >= min_speech_samples:
                        raw_spans.append((s_sample, e_sample))
                    in_speech = False
                    silence_frames = 0

    if in_speech:
        s_sample = start_idx * frame_samples
        e_sample = min(total_samples, (last_active_idx + 1) * frame_samples)
        if e_sample - s_sample >= min_speech_samples:
            raw_spans.append((s_sample, e_sample))

    return raw_spans


def _detect_speech_rms(
    samples: array.array,
    sample_rate: int,
    cfg: SesliNotConfig,
) -> list[tuple[int, int]]:
    """Deterministik çerçeve-tabanlı RMS + uyarlanabilir gürültü tabanı + histerezis VAD."""
    total_samples = len(samples)
    if total_samples == 0:
        return []

    frame_samples = max(1, int(sample_rate * _FRAME_MS / 1000))
    frame_dbs: list[float] = []
    for offset in range(0, total_samples, frame_samples):
        end = min(total_samples, offset + frame_samples)
        frame_dbs.append(_slice_rms_dbfs(samples, offset, end))

    if not frame_dbs:
        return []

    # 20. yüzdelik ile başlangıç gürültü tabanı tahmini; tamamen konuşma dolu dosyalarda
    # tavan (-40 dBFS) ile kırpılır ki sürekli konuşma gürültü sanılmasın.
    sorted_dbs = sorted(frame_dbs)
    p20 = sorted_dbs[int(0.20 * (len(sorted_dbs) - 1))]
    noise_floor = max(-65.0, min(-40.0, p20))

    min_silence_frames = max(1, int(math.ceil(cfg.audio.vad_min_silence_ms / _FRAME_MS)))
    min_speech_samples = int((cfg.audio.vad_min_speech_ms / 1000.0) * sample_rate)

    raw_spans: list[tuple[int, int]] = []
    in_speech = False
    start_idx = 0
    last_active_idx = 0
    silence_frames = 0

    for idx, db in enumerate(frame_dbs):
        onset_thresh = max(noise_floor + 10.0, -42.0)
        offset_thresh = onset_thresh - 5.0

        if not in_speech:
            if db >= onset_thresh:
                in_speech = True
                start_idx = idx
                last_active_idx = idx
                silence_frames = 0
            else:
                noise_floor = max(-65.0, min(-40.0, (0.98 * noise_floor) + (0.02 * db)))
        else:
            if db >= offset_thresh:
                last_active_idx = idx
                silence_frames = 0
            else:
                silence_frames += 1
                if silence_frames >= min_silence_frames:
                    s_sample = start_idx * frame_samples
                    e_sample = min(total_samples, (last_active_idx + 1) * frame_samples)
                    if e_sample - s_sample >= min_speech_samples:
                        raw_spans.append((s_sample, e_sample))
                    in_speech = False
                    silence_frames = 0

    if in_speech:
        s_sample = start_idx * frame_samples
        e_sample = min(total_samples, (last_active_idx + 1) * frame_samples)
        if e_sample - s_sample >= min_speech_samples:
            raw_spans.append((s_sample, e_sample))

    return raw_spans


def _pad_and_coalesce(
    spans: list[tuple[int, int]],
    total_samples: int,
    pad_samples: int,
) -> list[tuple[int, int]]:
    if not spans:
        return []
    padded = [
        (max(0, s - pad_samples), min(total_samples, e + pad_samples))
        for s, e in spans
    ]
    merged: list[tuple[int, int]] = [padded[0]]
    for s, e in padded[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


def _split_long_span(
    samples: array.array,
    start: int,
    end: int,
    sample_rate: int,
    max_secs: float,
) -> list[tuple[int, int]]:
    """`max_secs` (28sn) üstü kesintisiz konuşmayı [18sn, 28sn] en düşük RMS noktasından böler."""
    max_samples = int(max_secs * sample_rate)
    if end - start <= max_samples:
        return [(start, end)]

    overlap_samples = int(_LONG_SPLIT_OVERLAP_SECS * sample_rate)
    search_start_secs = (
        _LONG_SPLIT_SEARCH_START_SECS
        if max_secs >= 22.0
        else max(1.0, max_secs * 0.65)
    )
    search_lo_offset = int(search_start_secs * sample_rate)
    win_samples = max(1, int(0.100 * sample_rate))  # 100ms kayan RMS penceresi
    step_samples = max(1, int(0.020 * sample_rate))  # 20ms adım

    pieces: list[tuple[int, int]] = []
    cur = start

    while end - cur > max_samples:
        win_lo = min(end, cur + search_lo_offset)
        win_hi = min(end, cur + max_samples)
        if win_hi <= win_lo:
            split_pos = win_hi
        else:
            best_pos = win_hi
            best_db = float("inf")
            pos = win_lo
            while pos <= win_hi:
                w_start = max(cur, pos - win_samples // 2)
                w_end = min(win_hi, w_start + win_samples)
                db = _slice_rms_dbfs(samples, w_start, w_end)
                if db < best_db:
                    best_db = db
                    best_pos = pos
                pos += step_samples
            split_pos = min(best_pos, cur + max_samples)

        pieces.append((cur, split_pos))
        next_cur = split_pos - overlap_samples
        # İlerleme garantisi (sonsuz döngü koruması)
        if next_cur <= cur:
            next_cur = cur + max(step_samples, max_samples // 2)
        cur = next_cur

    if cur < end:
        pieces.append((cur, end))
    return pieces


def _greedy_merge_spans(
    spans: list[tuple[int, int]],
    sample_rate: int,
    target_secs: float,
    max_secs: float,
) -> list[tuple[int, int]]:
    """Ardışık konuşma parçalarını `target_secs` (22sn) hedefleyip `max_secs` (28sn) altında birleştirir."""
    if not spans:
        return []

    target_samples = int(min(target_secs, max_secs) * sample_rate)
    max_samples = int(max_secs * sample_rate)
    max_gap_samples = int(_MAX_MERGE_GAP_SECS * sample_rate)
    short_gap_samples = int(2.0 * sample_rate)

    merged: list[tuple[int, int]] = []
    cur_s, cur_e = spans[0]

    for ns, ne in spans[1:]:
        # Örtüşen (uzun blok bölünmesinden gelen) parçaları geri birleştirme
        if ns < cur_e:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = ns, ne
            continue

        gap = ns - cur_e
        combined = ne - cur_s
        if gap <= max_gap_samples and combined <= target_samples:
            cur_e = ne
        elif (
            gap <= short_gap_samples
            and combined <= max_samples
            and (cur_e - cur_s) < int(target_samples * 0.75)
        ):
            cur_e = ne
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = ns, ne

    merged.append((cur_s, cur_e))
    return merged


def segment_wav(wav_path: Path, cfg: SesliNotConfig) -> list[tuple[SpeechSegment, Path]]:
    """16kHz mono PCM WAV dosyasını VAD ile konuşma parçalarına (<=28sn) böler.

    Toplam konuşma süresi 0.3s altındaysa boş liste döndürerek Whisper sessizlik
    halüsinasyonlarını engeller.
    """
    wav_path = Path(wav_path)
    samples, sample_rate = _read_mono_s16_wav(wav_path)
    total_samples = len(samples)
    if total_samples == 0 or sample_rate <= 0:
        return []

    raw_spans: list[tuple[int, int]] | None = None
    onnx_model = _find_silero_onnx_model(cfg)
    if onnx_model is not None:
        raw_spans = _detect_speech_silero_onnx(samples, sample_rate, onnx_model, cfg)

    if raw_spans is None:
        raw_spans = _detect_speech_rms(samples, sample_rate, cfg)

    if not raw_spans:
        return []

    total_speech_secs = sum(e - s for s, e in raw_spans) / sample_rate
    if total_speech_secs < _MIN_TOTAL_SPEECH_SECS:
        return []

    pad_samples = int((cfg.audio.vad_pad_ms / 1000.0) * sample_rate)
    padded_spans = _pad_and_coalesce(raw_spans, total_samples, pad_samples)

    max_secs = min(float(cfg.audio.vad_max_secs), _HARD_WHISPER_CAP_SECS)
    target_secs = min(float(cfg.audio.vad_target_secs), max_secs)

    # 28sn üstü kesintisiz blokları en düşük RMS noktasından 0.4sn örtüşme ile böl
    capped_spans: list[tuple[int, int]] = []
    for s, e in padded_spans:
        capped_spans.extend(_split_long_span(samples, s, e, sample_rate, max_secs))

    final_spans = _greedy_merge_spans(capped_spans, sample_rate, target_secs, max_secs)

    chunks_dir = wav_path.parent / f"{wav_path.stem}_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for stale in chunks_dir.glob("chunk_*.wav"):
        stale.unlink(missing_ok=True)

    results: list[tuple[SpeechSegment, Path]] = []
    for idx, (s, e) in enumerate(final_spans):
        chunk_path = chunks_dir / f"chunk_{idx:03d}.wav"
        _write_pcm_chunk(chunk_path, samples[s:e], sample_rate)
        seg = SpeechSegment(
            start_sec=round(s / sample_rate, 3),
            end_sec=round(e / sample_rate, 3),
            rms_dbfs=round(_slice_rms_dbfs(samples, s, e), 2),
        )
        results.append((seg, chunk_path))

    return results
