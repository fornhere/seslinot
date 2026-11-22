"""OpenAI-uyumlu yerel Whisper STT istemcisi, halüsinasyon filtresi ve Voxtype yedeği."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import wave
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

import httpx

from seslinot.audio.vad import segment_wav
from seslinot.config import SesliNotConfig
from seslinot.models import TranscriptChunk, TranscriptionResult

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]+")
_NON_ALNUM_SPACE_RE = re.compile(r"[^0-9a-zçğıöşü\s]+")
_MULTI_SPACE_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[.!?,;:\n]+")

_LOW_ENERGY_DBFS = -38.0

# Kesin bilinen Türkçe Whisper sessizlik/YouTube halüsinasyon ifadeleri (normalize edilmiş)
_EXACT_HALLUCINATIONS = frozenset(
    {
        "altyazı m k",
        "altyazı mk",
        "altyazı",
        "türkçe altyazı",
        "izlediğiniz için teşekkürler",
        "izlediğiniz için teşekkür ederim",
        "izlediğiniz için çok teşekkürler",
        "izlediğiniz için çok teşekkür ederim",
        "dinlediğiniz için teşekkürler",
        "dinlediğiniz için teşekkür ederim",
        "abone olmayı unutmayın",
        "kanalıma abone olmayı unutmayın",
        "kanala abone olmayı unutmayın",
        "videoyu beğenmeyi unutmayın",
        "videoyu beğenmeyi ve abone olmayı unutmayın",
        "beğenmeyi ve abone olmayı unutmayın",
        "bir sonraki videoda görüşmek üzere",
        "görüşmek üzere",
        "müzik",
        "sessizlik",
        "alkış",
        "gülüşmeler",
        "music",
        "silence",
        "applause",
        "thank you for watching",
        "thanks for watching",
        "subtitles by",
    }
)

# Kısa parçalar içinde geçtiğinde kesin halüsinasyon sayılan kalıplar
_SUBSTRING_HALLUCINATIONS = (
    "altyazı m k",
    "altyazı mk",
    "izlediğiniz için teşekkür",
    "abone olmayı unutmayın",
    "kanalıma abone ol",
    "videoyu beğenmeyi unutma",
)

# Yalnızca düşük enerjili (<-38 dBFS) parçalarda elenecek kısa dolgu/hayalet ifadeler
_LOW_ENERGY_FILLERS = frozenset(
    {
        "teşekkürler",
        "teşekkür ederim",
        "sağ olun",
        "evet",
        "tamam",
        "hmm",
        "hm",
        "ıı",
        "ııı",
        "aa",
        "aaa",
        "ee",
        "eee",
        "uh",
        "um",
    }
)


class STTError(RuntimeError):
    """Konuşmadan metne dönüştürme (STT) hatası."""


def _normalize_tr(text: str) -> str:
    """Türkçe büyük/küçük harf duyarlı normalizasyon (İ->i, I->ı) ve noktalama temizliği."""
    lowered = text.replace("İ", "i").replace("I", "ı").lower()
    cleaned = _NON_ALNUM_SPACE_RE.sub(" ", lowered)
    return _MULTI_SPACE_RE.sub(" ", cleaned).strip()


def _tokenize_tr(text: str) -> list[str]:
    norm = _normalize_tr(text)
    return norm.split() if norm else []


def has_repetition_loop(text: str) -> bool:
    """Whisper sıcaklık=0 tekrar döngülerini tespit eder."""
    words = _tokenize_tr(text)
    if len(words) < 5:
        return False

    # 1. Aynı tek kelimenin art arda >=5 kez tekrarı
    run = 1
    for i in range(1, len(words)):
        if words[i] == words[i - 1]:
            run += 1
            if run >= 5:
                return True
        else:
            run = 1

    # 2. Aynı n-gram'ın (2..8 kelime) art arda >=3 kez tekrarı
    max_n = min(8, len(words) // 3)
    for n in range(2, max_n + 1):
        i = 0
        while i <= len(words) - 3 * n:
            gram = words[i : i + n]
            reps = 1
            j = i + n
            while j + n <= len(words) and words[j : j + n] == gram:
                reps += 1
                j += n
            if reps >= 3:
                return True
            i += 1

    # 3. Toplamda baskın n-gram tekrarı (>=4 kez ve metnin >=%50'sini kaplıyorsa)
    if len(words) >= 8:
        for n in range(2, min(6, len(words) // 4) + 1):
            counts: dict[tuple[str, ...], int] = {}
            for i in range(len(words) - n + 1):
                g = tuple(words[i : i + n])
                counts[g] = counts.get(g, 0) + 1
            top_count = max(counts.values(), default=0)
            if top_count >= 4 and (top_count * n) / len(words) >= 0.50:
                return True

    # 4. Aynı cümle/ibarenin >=3 kez tekrarı
    clauses = [_normalize_tr(c) for c in _CLAUSE_SPLIT_RE.split(text)]
    clauses = [c for c in clauses if len(c) >= 10]
    if len(clauses) >= 3:
        clause_counts: dict[str, int] = {}
        for c in clauses:
            clause_counts[c] = clause_counts.get(c, 0) + 1
            if clause_counts[c] >= 3:
                return True

    # 5. Uzun parçada aşırı düşük benzersiz kelime oranı
    if len(words) >= 16 and (len(set(words)) / len(words)) < 0.25:
        return True

    return False


def is_hallucination(text: str, rms_dbfs: float | None = None) -> bool:
    """Bilinen Türkçe Whisper halüsinasyonlarını ve düşük enerjili hayalet çıktıları yakalar."""
    stripped = text.strip()
    if not stripped:
        return True

    norm = _normalize_tr(stripped)
    # Yalnızca noktalama / sembol (örn. "...", "♪", "!!!")
    if not norm:
        return True

    if norm in _EXACT_HALLUCINATIONS:
        return True

    words = norm.split()
    if len(words) <= 8:
        for sig in _SUBSTRING_HALLUCINATIONS:
            if sig in norm:
                return True

    # Aynı kelimenin art arda 4+ kez tekrarlanması (enerjiden bağımsız)
    if len(words) >= 4 and len(set(words)) == 1:
        return True

    low_energy = rms_dbfs is not None and rms_dbfs < _LOW_ENERGY_DBFS
    if low_energy:
        if norm in _LOW_ENERGY_FILLERS:
            return True
        if len(norm) <= 2:
            return True
        # Düşük enerjide tek kelimenin 2+ tekrarı ("bir bir", "evet evet")
        if len(words) >= 2 and len(set(words)) == 1:
            return True

    return False


def join_transcript_chunks(texts: Sequence[str]) -> str:
    """Temiz parça metinlerini akıllı boşluk ve bigram/n-gram örtüşme temizliği ile birleştirir."""
    cleaned_parts = [_MULTI_SPACE_RE.sub(" ", t.strip()) for t in texts if t and t.strip()]
    if not cleaned_parts:
        return ""

    output_tokens: list[str] = cleaned_parts[0].split()
    for part in cleaned_parts[1:]:
        curr_tokens = part.split()
        if not curr_tokens:
            continue

        prev_norm = [_normalize_tr(tok) for tok in output_tokens[-4:]]
        curr_norm = [_normalize_tr(tok) for tok in curr_tokens[:4]]

        overlap = 0
        max_k = min(4, len(prev_norm), len(curr_norm))
        for k in range(max_k, 1, -1):  # 4-gram, 3-gram, bigram örtüşme temizliği
            if prev_norm[-k:] == curr_norm[:k] and all(prev_norm[-k:]):
                overlap = k
                break

        output_tokens.extend(curr_tokens[overlap:])

    return _MULTI_SPACE_RE.sub(" ", " ".join(output_tokens)).strip()


def _wav_duration_secs(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        frames = wf.getnframes()
        return (frames / rate) if rate > 0 else 0.0


class STTClient:
    """VAD + yerel OpenAI-uyumlu Whisper HTTP STT istemcisi (Voxtype CLI yedekli)."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg
        self._http_client = http_client

    def _post_chunk(
        self,
        client: httpx.Client,
        chunk_path: Path,
        *,
        temperature: float,
    ) -> str:
        audio_bytes = chunk_path.read_bytes()
        files = {"file": (chunk_path.name, audio_bytes, "audio/wav")}
        data = {
            "model": self.cfg.audio.stt_model,
            "language": self.cfg.audio.language,
            "prompt": self.cfg.audio.initial_prompt,
            "temperature": f"{temperature:.1f}",
            "response_format": "json",
        }
        resp = client.post(
            self.cfg.audio.stt_endpoint,
            files=files,
            data=data,
        )
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"STT sunucusu {resp.status_code} hatası döndürdü",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 400:
            raise STTError(
                f"STT istemci hatası ({resp.status_code}): {resp.text[:300]}"
            )

        content_type = resp.headers.get("content-type", "")
        if "json" in content_type.lower() or resp.text.lstrip().startswith("{"):
            payload = resp.json()
            if isinstance(payload, dict):
                return str(payload.get("text", "")).strip()
        return resp.text.strip()

    def _transcribe_via_voxtype(self, chunk_path: Path) -> str:
        voxtype_bin = shutil.which("voxtype")
        if not voxtype_bin:
            raise STTError("voxtype yedek ikili dosyası PATH üzerinde bulunamadı.")

        try:
            proc = subprocess.run(
                [voxtype_bin, "transcribe", str(chunk_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=self.cfg.audio.stt_timeout_secs,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise STTError(f"voxtype transcribe çalıştırılamadı: {exc}") from exc

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:300]
            raise STTError(f"voxtype transcribe başarısız (rc={proc.returncode}): {err}")

        return proc.stdout.strip()

    def transcribe_wav(self, wav_path: Path) -> TranscriptionResult:
        """WAV dosyasını VAD ile <=28sn parçalara ayırır ve metne dönüştürür."""
        wav_path = Path(wav_path)
        duration = _wav_duration_secs(wav_path)
        segments = segment_wav(wav_path, self.cfg)

        if not segments:
            return TranscriptionResult(
                text="",
                chunks=[],
                duration_secs=round(duration, 2),
                language=self.cfg.audio.language,
                stt_model=self.cfg.audio.stt_model,
                backend="openai_http",
            )

        chunks_dir = wav_path.parent / f"{wav_path.stem}_chunks"
        chunks: list[TranscriptChunk] = []
        backend_used = "openai_http"
        use_voxtype_fallback = False

        client_cm = (
            nullcontext(self._http_client)
            if self._http_client is not None
            else httpx.Client(timeout=self.cfg.audio.stt_timeout_secs)
        )

        try:
            with client_cm as client:
                assert client is not None
                for idx, (seg, chunk_path) in enumerate(segments):
                    raw_text: str
                    if use_voxtype_fallback:
                        raw_text = self._transcribe_via_voxtype(chunk_path)
                    else:
                        try:
                            raw_text = self._post_chunk(client, chunk_path, temperature=0.0)
                            if has_repetition_loop(raw_text):
                                logger.info(
                                    "Parça %d tekrar döngüsü tespit edildi, temperature=0.2 ile yenileniyor.",
                                    idx,
                                )
                                retry_text = self._post_chunk(client, chunk_path, temperature=0.2)
                                if retry_text and (
                                    not has_repetition_loop(retry_text)
                                    or len(retry_text) < len(raw_text)
                                ):
                                    raw_text = retry_text
                        except (
                            httpx.ConnectError,
                            httpx.TimeoutException,
                            httpx.NetworkError,
                            httpx.RemoteProtocolError,
                            httpx.HTTPStatusError,
                        ) as exc:
                            if self.cfg.audio.stt_fallback_voxtype and shutil.which("voxtype"):
                                logger.warning(
                                    "STT HTTP bağlantısı başarısız (%s); voxtype CLI yedeğine geçiliyor.",
                                    exc,
                                )
                                use_voxtype_fallback = True
                                backend_used = "voxtype_cli"
                                raw_text = self._transcribe_via_voxtype(chunk_path)
                            else:
                                raise STTError(f"STT HTTP sunucusuna ulaşılamadı: {exc}") from exc

                    hallucinated = is_hallucination(raw_text, seg.rms_dbfs)
                    chunks.append(
                        TranscriptChunk(
                            index=idx,
                            start_sec=seg.start_sec,
                            end_sec=seg.end_sec,
                            text=raw_text,
                            rms_dbfs=seg.rms_dbfs,
                            filtered_hallucination=hallucinated,
                        )
                    )
        finally:
            shutil.rmtree(chunks_dir, ignore_errors=True)

        clean_texts = [
            c.text for c in chunks if not c.filtered_hallucination and c.text.strip()
        ]
        joined = join_transcript_chunks(clean_texts)

        return TranscriptionResult(
            text=joined,
            chunks=chunks,
            duration_secs=round(duration, 2),
            language=self.cfg.audio.language,
            stt_model=self.cfg.audio.stt_model,
            backend=backend_used,
        )
