"""Çapraz platform (Windows/macOS/Linux) PortAudio (`sounddevice`) WAV kayıt işçisi.

`AudioCaptureController` tarafından ayrık alt süreç olarak başlatılır:
    python -m seslinot.audio._sd_record <out_wav> <sample_rate> <channels> <stop_flag_path>

Durdurma bayrağı dosyası oluşturulduğunda veya SIGINT/SIGTERM alındığında
WAV başlığını temiz biçimde kapatıp çıkar.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
import wave
from pathlib import Path


def run_sounddevice_recorder(
    out_wav: Path,
    sample_rate: int = 16000,
    channels: int = 1,
    stop_flag: Path | None = None,
) -> int:
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.stderr.write(
            "sounddevice paketi kurulu değil. Kurmak için: pip install 'seslinot[desktop]'\n"
            f"Ayrıntı: {exc}\n"
        )
        return 2

    stop_event = threading.Event()

    def _handle_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_stop)
            except (ValueError, OSError):
                pass

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit signed PCM
        wf.setframerate(sample_rate)

        def _callback(indata: bytes, _frames: int, _time_info: object, _status: object) -> None:
            if not stop_event.is_set():
                wf.writeframes(bytes(indata))

        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            callback=_callback,
        ):
            while not stop_event.is_set():
                if stop_flag is not None and stop_flag.exists():
                    stop_event.set()
                    break
                time.sleep(0.05)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        sys.stderr.write(
            "Kullanım: python -m seslinot.audio._sd_record <out_wav> <rate> <channels> [stop_flag]\n"
        )
        return 2
    out_wav = Path(args[0])
    rate = int(args[1])
    channels = int(args[2])
    stop_flag = Path(args[3]) if len(args) >= 4 and args[3] else None
    return run_sounddevice_recorder(out_wav, rate, channels, stop_flag)


if __name__ == "__main__":
    raise SystemExit(main())
