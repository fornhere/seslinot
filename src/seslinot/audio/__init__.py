"""SesliNot ses yakalama, sesli geri bildirim, VAD ve yerel STT paketi."""

from __future__ import annotations

from seslinot.audio.capture import (
    AudioCaptureController,
    CaptureError,
    archive_or_cleanup_audio,
    merge_wav_parts,
)
from seslinot.audio.cues import (
    CueKind,
    PipeWireDucker,
    ensure_cue_sounds,
    play_cue,
)
from seslinot.audio.stt import (
    STTClient,
    STTError,
    has_repetition_loop,
    is_hallucination,
    join_transcript_chunks,
)
from seslinot.audio.vad import (
    SpeechSegment,
    segment_wav,
)

__all__ = [
    "AudioCaptureController",
    "CaptureError",
    "CueKind",
    "PipeWireDucker",
    "STTClient",
    "STTError",
    "SpeechSegment",
    "archive_or_cleanup_audio",
    "ensure_cue_sounds",
    "has_repetition_loop",
    "is_hallucination",
    "join_transcript_chunks",
    "merge_wav_parts",
    "play_cue",
    "segment_wav",
]
