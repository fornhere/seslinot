"""Ses yakalama, sesli geri bildirim, PipeWire ducking, VAD ve yerel STT birim testleri."""

from __future__ import annotations

import json
import math
import os
import signal
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from seslinot.audio import (
    AudioCaptureController,
    CaptureError,
    PipeWireDucker,
    STTClient,
    STTError,
    archive_or_cleanup_audio,
    ensure_cue_sounds,
    has_repetition_loop,
    is_hallucination,
    join_transcript_chunks,
    merge_wav_parts,
    play_cue,
    segment_wav,
)
from seslinot.config import SesliNotConfig


# --- Sentetik WAV Yardımcıları ---


def _make_cfg(tmp_path: Path) -> SesliNotConfig:
    cfg = SesliNotConfig()
    cfg.vault.path = tmp_path / "vault"
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.paths.ensure_dirs()
    return cfg


def _write_wav_segments(
    path: Path,
    segments: list[tuple[float, float, float]],
    *,
    sample_rate: int = 16_000,
) -> Path:
    """(duration_sec, amplitude_0_to_1, freq_hz) dilimlerinden 16kHz mono PCM WAV üretir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = bytearray()
    for dur, amp, freq in segments:
        n = int(round(dur * sample_rate))
        if amp <= 0.0 or freq <= 0.0:
            pcm.extend(b"\x00\x00" * n)
        else:
            step = 2.0 * math.pi * freq / sample_rate
            scale = max(0.0, min(1.0, amp)) * 32767.0
            for i in range(n):
                val = int(round(scale * math.sin(step * i)))
                pcm.extend(struct.pack("<h", val))

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm))
    return path


# --- Cue Tone & Playback Testleri ---


def test_ensure_cue_sounds_generates_valid_48khz_mono_wavs(tmp_path: Path) -> None:
    sounds_dir = tmp_path / "sounds"
    cues = ensure_cue_sounds(sounds_dir)
    assert set(cues.keys()) == {"start", "stop", "cancel", "done"}

    expected_ms = {"start": 90, "stop": 100, "cancel": 110, "done": 140}
    mtimes: dict[str, int] = {}

    for kind, wav_path in cues.items():
        assert wav_path.is_file()
        mtimes[kind] = wav_path.stat().st_mtime_ns
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 48_000
            dur_ms = (wf.getnframes() / wf.getframerate()) * 1000.0
            assert abs(dur_ms - expected_ms[kind]) < 5.0
            raw = wf.readframes(wf.getnframes())
            samples = struct.unpack(f"<{wf.getnframes()}h", raw)
            peak = max(abs(s) for s in samples)
            assert peak > 1000

    # İkinci çağrı mevcut dosyaları yeniden yazmamalı (idempotent)
    cues_again = ensure_cue_sounds(sounds_dir)
    for kind, wav_path in cues_again.items():
        assert wav_path.stat().st_mtime_ns == mtimes[kind]


def test_play_cue_start_blocking_settle_and_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    run_calls: list[list[str]] = []
    popen_calls: list[list[str]] = []
    sleeps: list[float] = []

    monkeypatch.setattr("seslinot.audio.cues.shutil.which", lambda name: f"/usr/bin/{name}" if name == "pw-play" else None)
    monkeypatch.setattr(
        "seslinot.audio.cues.subprocess.run",
        lambda cmd, **kw: run_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )
    monkeypatch.setattr(
        "seslinot.audio.cues.subprocess.Popen",
        lambda cmd, **kw: popen_calls.append(list(cmd)),
    )
    monkeypatch.setattr("seslinot.audio.cues.time.sleep", lambda secs: sleeps.append(secs))

    # start -> varsayılan blocking=True + 80ms bekleme
    assert play_cue("start", cfg) is True
    assert len(run_calls) == 1
    assert run_calls[0][:7] == [
        "pw-play",
        "--volume",
        "0.55",
        "--media-category",
        "Playback",
        "--media-role",
        "Notification",
    ]
    assert sleeps == [pytest.approx(0.08)]

    # stop -> varsayılan blocking=False (asenkron)
    assert play_cue("stop", cfg) is True
    assert len(popen_calls) == 1
    assert popen_calls[0][0] == "pw-play"

    # paplay yedeği
    monkeypatch.setattr("seslinot.audio.cues.shutil.which", lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    assert play_cue("done", cfg, blocking=True) is True
    assert run_calls[-1][0] == "paplay"

    # Hiç oynatıcı yoksa sessiz no-op
    monkeypatch.setattr("seslinot.audio.cues.shutil.which", lambda _name: None)
    assert play_cue("cancel", cfg) is False

    # Devre dışıyken no-op
    cfg.audio.cues_enabled = False
    assert play_cue("start", cfg) is False


# --- PipeWireDucker & Crash-Recovery Testleri ---


def test_pipewire_ducker_exclusions_and_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "duck_state.json"
    sink_inputs_payload = [
        {
            "index": 101,
            "corked": False,
            "volume": {
                "front-left": {"value": 65536, "value_percent": "100%"},
                "front-right": {"value": 65536, "value_percent": "100%"},
            },
            "properties": {
                "application.name": "Firefox",
                "media.role": "video",
            },
        },
        {
            "index": 102,
            "corked": False,
            "volume": {"mono": {"value": 65536}},
            "properties": {
                "application.name": "notify-send",
                "media.role": "notification",
            },
        },
        {
            "index": 103,
            "corked": False,
            "volume": {"mono": {"value": 65536}},
            "properties": {
                "application.name": "System",
                "media.role": "event",
            },
        },
        {
            "index": 104,
            "corked": False,
            "volume": {"mono": {"value": 50000}},
            "properties": {
                "application.name": "SesliNot",
                "media.role": "music",
            },
        },
        {
            "index": 105,
            "corked": True,
            "volume": {"front-left": {"value": 65536}},
            "properties": {
                "application.name": "Spotify",
                "media.role": "music",
            },
        },
    ]

    set_vol_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["pactl", "--format=json", "list", "sink-inputs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(sink_inputs_payload), stderr="")
        if cmd[:2] == ["pactl", "set-sink-input-volume"]:
            set_vol_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Beklenmeyen komut: {cmd}")

    monkeypatch.setattr("seslinot.audio.cues.shutil.which", lambda name: "/usr/bin/pactl" if name == "pactl" else None)
    monkeypatch.setattr("seslinot.audio.cues.subprocess.run", fake_run)

    ducker = PipeWireDucker(state_path, ratio=0.30)
    ducked = ducker.duck()
    assert ducked == 1
    assert state_path.is_file()

    # Sadece 101 (Firefox) kısılmış olmalı: round(65536 * 0.30) = 19661
    assert set_vol_calls == [["pactl", "set-sink-input-volume", "101", "19661", "19661"]]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["streams"] == {"101": [65536, 65536]}

    # Tekrar duck() çifte kısma yapmamalı ve orijinal sesi korumalı
    assert ducker.duck() == 0
    assert len(set_vol_calls) == 1

    # Çökme sonrası kurtarma: yeni bir PipeWireDucker örneği state dosyasından geri yükler
    set_vol_calls.clear()
    recovered_ducker = PipeWireDucker(state_path, ratio=0.30)
    restored = recovered_ducker.restore()
    assert restored == 1
    assert set_vol_calls == [["pactl", "set-sink-input-volume", "101", "65536", "65536"]]
    assert not state_path.exists()

    # Yetim dosya yokken restore() güvenli no-op
    assert recovered_ducker.restore() == 0


# --- WAV Birleştirme ve AudioCaptureController Testleri ---


def test_merge_wav_parts_exact_concatenation(tmp_path: Path) -> None:
    p1 = _write_wav_segments(tmp_path / "part_001.wav", [(0.5, 0.3, 440.0)])
    p2 = _write_wav_segments(tmp_path / "part_002.wav", [(0.3, 0.2, 660.0)])
    p3 = _write_wav_segments(tmp_path / "part_003.wav", [(0.4, 0.4, 330.0)])
    out = tmp_path / "merged.wav"

    merged = merge_wav_parts([p1, p2, p3], out)
    assert merged == out
    assert out.is_file()

    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        assert wf.getnframes() == int(round((0.5 + 0.3 + 0.4) * 16_000))
        merged_frames = wf.readframes(wf.getnframes())

    expected = bytearray()
    for p in (p1, p2, p3):
        with wave.open(str(p), "rb") as wf:
            expected.extend(wf.readframes(wf.getnframes()))
    assert merged_frames == bytes(expected)


def test_capture_controller_full_segmented_lifecycle_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    cue_log: list[tuple[str, bool | None]] = []
    duck_log: list[str] = []
    spawned_cmds: list[list[str]] = []
    signals_sent: list[tuple[int, int]] = []

    class FakePopen:
        _next_pid = 40000

        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            FakePopen._next_pid += 1
            self.pid = FakePopen._next_pid
            self.cmd = list(cmd)
            self.part_path = Path(cmd[-1])
            self._returncode: int | None = None
            spawned_cmds.append(self.cmd)

        def poll(self) -> int | None:
            return self._returncode

        def send_signal(self, sig: int) -> None:
            signals_sent.append((self.pid, sig))
            if sig == signal.SIGINT:
                _write_wav_segments(self.part_path, [(0.5, 0.35, 440.0)])
                self._returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0 if self._returncode is None else self._returncode

        def terminate(self) -> None:
            self._returncode = -15

        def kill(self) -> None:
            self._returncode = -9

    monkeypatch.setattr("seslinot.audio.capture.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("seslinot.audio.capture.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "seslinot.audio.capture.play_cue",
        lambda kind, _cfg, blocking=None: cue_log.append((kind, blocking)) or True,
    )

    ctrl = AudioCaptureController(cfg)
    monkeypatch.setattr(ctrl.ducker, "duck", lambda: duck_log.append("duck") or 1)
    monkeypatch.setattr(ctrl.ducker, "restore", lambda: duck_log.append("restore") or 1)

    assert ctrl.status()["state"] == "idle"
    assert ctrl.status()["recording_started_at"] == 0

    st1 = ctrl.start()
    assert st1["state"] == "recording"
    assert len(st1["parts"]) == 1
    assert cue_log == [("start", True)]
    assert duck_log == ["restore", "duck"]
    assert spawned_cmds[0][:15] == [
        "pw-record",
        "--rate",
        "16000",
        "--channels",
        "1",
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
    ]
    assert ctrl.status()["state"] == "recording"
    assert ctrl.status()["recording_started_at"] > 0

    # Aktif kayıt varken tekrar start() reddedilmeli
    with pytest.raises(CaptureError):
        ctrl.start()

    # Duraklat -> SIGINT + restore
    st2 = ctrl.pause()
    assert st2["state"] == "paused"
    assert st2["pid"] is None
    assert signals_sent[-1][1] == signal.SIGINT
    assert ctrl.status()["state"] == "paused"

    # Devam ettir -> part_002.wav
    st3 = ctrl.resume()
    assert st3["state"] == "recording"
    assert len(st3["parts"]) == 2
    assert Path(st3["parts"][1]).name == "part_002.wav"

    # Durdur -> spool WAV (0.5s + 0.5s = 1.0s)
    spool_wav = ctrl.stop()
    assert spool_wav.is_file()
    assert spool_wav.parent == cfg.paths.spool_dir
    with wave.open(str(spool_wav), "rb") as wf:
        assert wf.getnframes() == 16_000  # 1.0s
    assert not cfg.paths.capture_state_path.exists()
    assert not Path(st1["session_dir"]).exists()
    assert ("stop", False) in cue_log
    assert ctrl.status()["state"] == "idle"

    # İptal akışı
    st_cancel = ctrl.start()
    session_dir = Path(st_cancel["session_dir"])
    assert session_dir.is_dir()
    ctrl.cancel()
    assert not session_dir.exists()
    assert not cfg.paths.capture_state_path.exists()
    assert ("cancel", False) in cue_log
    assert ctrl.status()["state"] == "idle"


# --- Ses Saklama / Arşivleme Politikası Testleri ---


def test_archive_or_cleanup_retention_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)

    # 1. delete_after_verify
    cfg.audio.retention = "delete_after_verify"
    wav1 = _write_wav_segments(cfg.paths.spool_dir / "s1.wav", [(0.5, 0.2, 440.0)])
    assert archive_or_cleanup_audio(wav1, "sn-001", cfg) is None
    assert not wav1.exists()

    # 2. opus_30d: dönüşüm + spool silme + 30 günden eski opus temizliği
    cfg.audio.retention = "opus_30d"
    wav2 = _write_wav_segments(cfg.paths.spool_dir / "s2.wav", [(0.5, 0.2, 440.0)])

    old_opus = cfg.paths.audio_dir / "2025" / "01" / "old-note.opus"
    old_opus.parent.mkdir(parents=True, exist_ok=True)
    old_opus.write_bytes(b"OggS-old")
    old_ts = time.time() - (45 * 86_400)
    os.utime(old_opus, (old_ts, old_ts))

    recent_opus = cfg.paths.audio_dir / "2026" / "11" / "recent-note.opus"
    recent_opus.parent.mkdir(parents=True, exist_ok=True)
    recent_opus.write_bytes(b"OggS-recent")

    ffmpeg_cmds: list[list[str]] = []

    def fake_ffmpeg(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ffmpeg_cmds.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"OggS-converted-opus-data")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("seslinot.audio.capture.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("seslinot.audio.capture.subprocess.run", fake_ffmpeg)

    archived = archive_or_cleanup_audio(wav2, "sn-20261122-01", cfg)
    assert archived is not None
    assert archived.is_file()
    assert archived.suffix == ".opus"
    assert not wav2.exists()
    assert not old_opus.exists()
    assert recent_opus.exists()
    assert ffmpeg_cmds[0][:15] == [
        "ffmpeg",
        "-y",
        "-i",
        str(wav2),
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
    ]

    # 3. Fail-closed: ffmpeg hata verirse spool WAV silinmemeli!
    wav3 = _write_wav_segments(cfg.paths.spool_dir / "s3.wav", [(0.5, 0.2, 440.0)])
    monkeypatch.setattr(
        "seslinot.audio.capture.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="codec error"),
    )
    assert archive_or_cleanup_audio(wav3, "sn-fail", cfg) is None
    assert wav3.exists()


# --- VAD Segmentasyon Testleri ---


def test_vad_silence_and_short_blip_return_empty(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)

    silent_wav = _write_wav_segments(tmp_path / "silence.wav", [(3.0, 0.0, 0.0)])
    assert segment_wav(silent_wav, cfg) == []

    # 0.15s (<0.3s eşiği) kısa ses patlaması elenmeli
    short_wav = _write_wav_segments(
        tmp_path / "short.wav",
        [(1.0, 0.0, 0.0), (0.15, 0.4, 440.0), (1.0, 0.0, 0.0)],
    )
    assert segment_wav(short_wav, cfg) == []


def test_vad_speech_bursts_detection_and_greedy_merge(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    # 2 yakın konuşma (1.0s boşluk -> birleşir) + 6.0s sessizlik + 3. konuşma (ayrı parça)
    wav = _write_wav_segments(
        tmp_path / "bursts.wav",
        [
            (0.8, 0.0, 0.0),
            (2.0, 0.35, 350.0),
            (1.0, 0.0, 0.0),
            (2.2, 0.35, 420.0),
            (6.0, 0.0, 0.0),
            (2.0, 0.35, 390.0),
            (0.8, 0.0, 0.0),
        ],
    )
    chunks = segment_wav(wav, cfg)
    assert len(chunks) == 2
    seg0, path0 = chunks[0]
    seg1, path1 = chunks[1]

    assert path0.is_file() and path1.is_file()
    assert path0.parent == tmp_path / "bursts_chunks"
    assert seg0.duration_sec <= cfg.audio.vad_max_secs
    assert seg1.duration_sec <= cfg.audio.vad_max_secs
    assert seg0.rms_dbfs > -25.0
    assert seg1.rms_dbfs > -25.0
    assert seg1.start_sec > seg0.end_sec


def test_vad_continuous_36s_tone_splits_at_lowest_rms_under_28s_with_overlap(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    # 36sn kesintisiz ses; 22.0sn-22.6sn arasında belirgin düşük enerji çukuru var
    wav = _write_wav_segments(
        tmp_path / "long_speech.wav",
        [
            (22.0, 0.35, 360.0),
            (0.6, 0.04, 360.0),  # [18s, 28s] içinde en düşük RMS noktası (~22.3s)
            (13.4, 0.35, 360.0),
        ],
    )
    chunks = segment_wav(wav, cfg)
    assert len(chunks) == 2
    seg0, p0 = chunks[0]
    seg1, p1 = chunks[1]

    assert p0.is_file() and p1.is_file()
    # Whisper <30sn katı üst sınır garantisi
    assert seg0.duration_sec <= 28.0
    assert seg1.duration_sec <= 28.0
    # Bölünme düşük RMS çukuru civarında (21.8s .. 22.8s) gerçekleşmeli
    assert 21.8 <= seg0.end_sec <= 22.8
    # 0.4s örtüşme garantisi
    overlap = seg0.end_sec - seg1.start_sec
    assert 0.35 <= overlap <= 0.45


# --- Halüsinasyon, Tekrar Döngüsü ve STT İstemcisi Testleri ---


def test_hallucination_and_repetition_loop_filters() -> None:
    # Bilinen Türkçe Whisper halüsinasyonları
    assert is_hallucination("Altyazı M.K.", -22.0) is True
    assert is_hallucination("İzlediğiniz için teşekkürler!", -20.0) is True
    assert is_hallucination("Abone olmayı unutmayın.", -24.0) is True
    assert is_hallucination("[Müzik]", -18.0) is True
    assert is_hallucination("Sessizlik", -30.0) is True
    assert is_hallucination("... ♪ !!", -20.0) is True
    assert is_hallucination("bir bir bir bir", -20.0) is True

    # Düşük enerji eşiği (<-38 dBFS)
    assert is_hallucination("Teşekkürler.", -42.0) is True
    assert is_hallucination("evet evet", -40.0) is True
    assert is_hallucination("Teşekkürler.", -18.0) is False

    # Normal Türkçe sesli not cümlesi elenmemeli
    assert is_hallucination("PipeWire düşük gecikme ayarlarını araştır.", -18.0) is False

    # Tekrar döngüsü algılayıcı
    assert has_repetition_loop("ve ve ve ve ve") is True
    assert (
        has_repetition_loop(
            "bu konuyu araştır bu konuyu araştır bu konuyu araştır"
        )
        is True
    )
    assert (
        has_repetition_loop(
            "Bugün Claude Code ile yeni bir branch açtım ve testleri koşturdum."
        )
        is False
    )


def test_join_transcript_chunks_bigram_overlap_dedup() -> None:
    joined = join_transcript_chunks(
        [
            "Claude Code'da yeni bir branch",
            "bir branch açtım ve commit attım.",
            "commit attım. Ardından testleri çalıştırdım.",
        ]
    )
    assert joined == "Claude Code'da yeni bir branch açtım ve commit attım. Ardından testleri çalıştırdım."


def test_stt_client_http_retry_and_hallucination_filtering(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    # 3 ayrı parça üreten sentetik WAV (5s sessizlik aralarıyla)
    wav = _write_wav_segments(
        tmp_path / "note.wav",
        [
            (0.4, 0.0, 0.0),
            (1.5, 0.35, 350.0),
            (5.0, 0.0, 0.0),
            (1.5, 0.35, 420.0),
            (5.0, 0.0, 0.0),
            (1.5, 0.35, 390.0),
            (0.4, 0.0, 0.0),
        ],
    )

    seen_calls: list[tuple[str, str]] = []  # (filename, temperature)

    def handler(request: httpx.Request) -> httpx.Response:
        body_text = request.content.decode("latin1")
        # Hangi parça ve hangi temperature geldiğini multipart gövdeden çıkar
        fname = "chunk_000.wav"
        for candidate in ("chunk_000.wav", "chunk_001.wav", "chunk_002.wav"):
            if candidate in body_text:
                fname = candidate
                break
        temp = "0.2" if 'name="temperature"\r\n\r\n0.2' in body_text else "0.0"
        seen_calls.append((fname, temp))

        if fname == "chunk_000.wav" and temp == "0.0":
            return httpx.Response(
                200,
                json={"text": "pipewire ayarı pipewire ayarı pipewire ayarı"},
            )
        if fname == "chunk_000.wav" and temp == "0.2":
            return httpx.Response(200, json={"text": "Bugün PipeWire gecikme ayarlarını"})
        if fname == "chunk_001.wav":
            return httpx.Response(200, json={"text": "Altyazı M.K."})
        return httpx.Response(200, json={"text": "gecikme ayarlarını test ettim."})

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        client = STTClient(cfg, http_client=mock_http)
        result = client.transcribe_wav(wav)

    assert result.backend == "openai_http"
    assert len(result.chunks) == 3
    assert seen_calls == [
        ("chunk_000.wav", "0.0"),
        ("chunk_000.wav", "0.2"),  # Tekrar döngüsü nedeniyle temperature=0.2 yeniden denemesi
        ("chunk_001.wav", "0.0"),
        ("chunk_002.wav", "0.0"),
    ]
    assert result.chunks[1].filtered_hallucination is True
    # chunk 0 + chunk 2 bigram ("gecikme ayarlarını") örtüşmesi temizlenerek birleşmeli
    assert result.text == "Bugün PipeWire gecikme ayarlarını test ettim."
    # Geçici parça dizini temizlenmiş olmalı
    assert not (tmp_path / "note_chunks").exists()


def test_stt_client_voxtype_fallback_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    wav = _write_wav_segments(
        tmp_path / "fallback.wav",
        [(0.3, 0.0, 0.0), (1.5, 0.35, 440.0), (0.3, 0.0, 0.0)],
    )

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    voxtype_calls: list[list[str]] = []

    def fake_voxtype(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        voxtype_calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Yerel voxtype yedeği sorunsuz çalıştı.\n", stderr=""
        )

    monkeypatch.setattr("seslinot.audio.stt.shutil.which", lambda name: "/usr/bin/voxtype" if name == "voxtype" else None)
    monkeypatch.setattr("seslinot.audio.stt.subprocess.run", fake_voxtype)

    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as mock_http:
        client = STTClient(cfg, http_client=mock_http)
        result = client.transcribe_wav(wav)

    assert result.backend == "voxtype_cli"
    assert result.text == "Yerel voxtype yedeği sorunsuz çalıştı."
    assert len(voxtype_calls) == 1
    assert voxtype_calls[0][:2] == ["/usr/bin/voxtype", "transcribe"]

    # Fallback kapalıysa fail-closed STTError fırlatmalı
    cfg.audio.stt_fallback_voxtype = False
    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as mock_http:
        client = STTClient(cfg, http_client=mock_http)
        with pytest.raises(STTError):
            client.transcribe_wav(wav)


def test_stt_client_silence_skips_http_requests(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    wav = _write_wav_segments(tmp_path / "quiet.wav", [(2.0, 0.0, 0.0)])
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"text": "Altyazı M.K."})

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        result = STTClient(cfg, http_client=mock_http).transcribe_wav(wav)

    assert called is False
    assert result.text == ""
    assert result.chunks == []
    assert result.duration_secs == pytest.approx(2.0, abs=0.05)


def test_capture_stale_dead_pid_reports_idle(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.paths.capture_state_path.write_text(
        json.dumps(
            {
                "session_id": "sn-dead",
                "state": "recording",
                "pid": 99999999,
                "parts": [],
                "started_at": 1700000000,
                "session_dir": str(tmp_path / "dead"),
            }
        ),
        encoding="utf-8",
    )
    ctrl = AudioCaptureController(cfg)
    st = ctrl.status()
    assert st["state"] == "idle"
    assert st["recording_started_at"] == 0


def test_archive_opus_keeps_old_files_and_sanitizes_note_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.audio.retention = "opus"

    old_opus = cfg.paths.audio_dir / "2024" / "01" / "keep-me.opus"
    old_opus.parent.mkdir(parents=True, exist_ok=True)
    old_opus.write_bytes(b"OggS-old")
    old_ts = time.time() - (90 * 86_400)
    os.utime(old_opus, (old_ts, old_ts))

    wav = _write_wav_segments(cfg.paths.spool_dir / "s.wav", [(0.4, 0.25, 440.0)])

    def fake_ffmpeg(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"OggS-opus")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("seslinot.audio.capture.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("seslinot.audio.capture.subprocess.run", fake_ffmpeg)

    archived = archive_or_cleanup_audio(wav, "../../escape/sn-note-1", cfg)
    assert archived is not None
    assert archived.resolve().is_relative_to(cfg.paths.audio_dir.resolve())
    assert old_opus.exists()  # "opus" (süresiz) politikası eski dosyaları silmemeli


def test_integration_templates_valid() -> None:
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    service_path = repo_root / "integrations/systemd/whisper-server.service"
    snippet_path = repo_root / "integrations/voxtype/remote-snippet.toml"

    assert service_path.is_file()
    service_text = service_path.read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in service_text
    assert "--port 8178" in service_text
    assert "--inference-path /v1/audio/transcriptions" in service_text
    assert "ggml-large-v3-turbo.bin" in service_text
    assert "-l tr -fa --no-context" in service_text

    assert snippet_path.is_file()
    snippet = tomllib.loads(snippet_path.read_text(encoding="utf-8"))
    assert snippet["whisper"]["mode"] == "remote"
    assert snippet["whisper"]["remote_endpoint"] == "http://127.0.0.1:8178/v1"

