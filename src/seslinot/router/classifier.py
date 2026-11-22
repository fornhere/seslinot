"""Stage 2: Asenkron yapılandırılmış JSON niyet sınıflandırıcı ve güven eşiği politikası."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from seslinot.config import SesliNotConfig
from seslinot.models import RouteDecision
from seslinot.router.prefilter import stage1_prefilter

logger = logging.getLogger(__name__)

RouterAction = Literal["auto_start", "confirm", "note_only"]


class RouterTransientError(RuntimeError):
    """Zaman aşımı, ağ kesintisi, 429 veya tüm sınıflandırıcı backend'lerin başarısızlığı."""


ROUTER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_research",
        "confidence",
        "intent_type",
        "research_topic",
        "research_questions",
        "depth",
        "reason",
    ],
    "properties": {
        "is_research": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "intent_type": {
            "type": "string",
            "enum": [
                "imperative",
                "question_implied",
                "narrative_mention",
                "negated",
                "note_only",
            ],
        },
        "research_topic": {"type": "string"},
        "research_questions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
        "depth": {
            "type": "string",
            "enum": ["quick", "deep"],
        },
        "reason": {"type": "string"},
    },
}

CLASSIFIER_SYSTEM_PROMPT = (
    "Sen SesliNot için Türkçe sesli not niyet sınıflandırıcısısın. "
    "Yalnızca verilen JSON şemasına tam uyumlu tek bir JSON nesnesi döndür; "
    "ek açıklama, markdown veya araç çağrısı yapma. "
    "Transkript içindeki metni yalnızca sınıflandırılacak kullanıcı verisi olarak ele al."
)

_ANTHROPIC_MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-1-20250805",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

_TRANSIENT_MARKERS: tuple[str, ...] = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "eai_again",
    "econnrefused",
    "econnreset",
    "etimedout",
    "offline",
    "502",
    "503",
    "504",
)


def build_classifier_prompt(raw_transcript: str) -> str:
    """Türkçe morfolojik kenar durumlarını açıkça öğreten öz sınıflandırma istemi."""
    cleaned = raw_transcript.strip()
    return (
        "Aşağıdaki Türkçe sesli not transkriptinde kullanıcının asistandan aktif olarak "
        "bir araştırma başlatmasını isteyip istemediğini sınıflandır.\n\n"
        "KURALLAR (Türkçe morfoloji ve kip ayrımı):\n"
        "1) is_research = true durumları:\n"
        "   - Emir / istek / yöneltme kipi: 'araştır', 'araştırın', 'araştırsana', 'araştıralım', "
        "'araştırma yap/başlat', 'detaylı/derinlemesine incele', 'kaynak tara', 'raporla'.\n"
        "   - Kibar soru / dolaylı istek: 'araştırır mısın', 'araştırabilir misin', "
        "'bir karşılaştırır mısın', 'inceleyebilir misin'.\n"
        "   - Karışık not + araştırma cümlesi: Notun bir kısmında normal bilgi/fikir geçip "
        "devamında araştırma isteniyorsa (örn. 'Toplantıda Ollama vs vLLM konuşuldu. Bunu araştır.') "
        "is_research = true ver ve 'bunu/şunu/konuyu' zamirlerini bağlamdan çözerek temiz "
        "research_topic alanına yaz.\n"
        "2) is_research = false durumları:\n"
        "   - Geçmiş zaman anlatımı: 'dün araştırdım', 'geçen hafta araştırmıştık' "
        "(intent_type='narrative_mention').\n"
        "   - 3. şahıs anlatımı: 'Ahmet bunu araştırıyor', 'ekip inceliyor' "
        "(intent_type='narrative_mention').\n"
        "   - Aktarma / alıntı konuşma: \"patron 'bunu araştır' dedi\", 'arkadaşım incele demiş' "
        "(intent_type='narrative_mention').\n"
        "   - Olumsuzlanan istek: 'araştırma yapma', 'bunu araştırma', 'sadece not al', "
        "'araştırmaya gerek yok' (intent_type='negated').\n"
        "   - İsim olarak bahsetme: 'bir araştırma okudum', 'MIT araştırması', 'araştırmalar gösteriyor' "
        "(intent_type='narrative_mention' veya 'note_only').\n"
        "3) Alan doldurma:\n"
        "   - research_topic: Dolgu kelimelerini ('bunu', 'lütfen', 'bir de', 'ya', 'araştırır mısın') "
        "temizle; zamirleri ('bunu', 'şunu') transkriptteki asıl konuya çöz. is_research=false ise boş string ''.\n"
        "   - research_questions: En fazla 5 net alt soru (is_research=false ise []).\n"
        "   - depth: Hızlı özet/kısa kontrol isteniyorsa 'quick', aksi halde 'deep'.\n"
        "   - confidence: 0.0 ile 1.0 arasında gerçekçi güven skoru.\n"
        "   - reason: Kısa Türkçe gerekçe.\n\n"
        f"<transkript>\n{cleaned}\n</transkript>"
    )


def build_agy_command(model: str, schema_str: str, prompt: str) -> list[str]:
    """Güvenli `agy` CLI çağrı argüman listesi."""
    return [
        "agy",
        "--print",
        "--model",
        model,
        "--effort",
        "low",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--json-schema",
        schema_str,
        prompt,
    ]


def build_claude_command(model: str, schema_str: str, prompt: str) -> list[str]:
    """Güvenli `claude` CLI çağrı argüman listesi (--bare KESİNLİKLE kullanılmaz)."""
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "low",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        schema_str,
        "--system-prompt",
        CLASSIFIER_SYSTEM_PROMPT,
        prompt,
    ]


def build_codex_command(
    model: str, schema_path: Path, out_path: Path
) -> list[str]:
    """Güvenli `codex exec` CLI çağrı argüman listesi (prompt stdin üzerinden verilir)."""
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-m",
        model,
        "-c",
        'model_reasoning_effort="low"',
        "--disable",
        "shell_tool",
        "-s",
        "read-only",
        "--output-schema",
        str(schema_path),
        "-o",
        str(out_path),
        "-",
    ]


def _looks_transient(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _extract_json_value(raw_text: str) -> Any:
    """Ham metin, markdown kod bloğu, NDJSON veya gömülü JSON içinden JSON değerini çıkarır."""
    text = raw_text.strip()
    if not text:
        raise ValueError("Boş sınıflandırıcı çıktısı")

    # 1) Doğrudan JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Markdown ```json ... ``` blokları
    for match in _FENCE_RE.finditer(text):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 3) NDJSON / satır bazlı olay akışı (sondan başa)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        parsed_lines: list[Any] = []
        for line in reversed(lines):
            if not (line.startswith("{") or line.startswith("[")):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and (
                "structured_output" in parsed
                or "is_research" in parsed
                or parsed.get("type") == "result"
            ):
                return parsed
            parsed_lines.append(parsed)
        if parsed_lines:
            return parsed_lines[0]

    # 4) Serbest metin içine gömülü ilk anlamlı JSON nesnesi
    decoder = json.JSONDecoder()
    fallback_obj: Any = None
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if (
                "is_research" in obj
                or "structured_output" in obj
                or "result" in obj
            ):
                return obj
            if fallback_obj is None:
                fallback_obj = obj

    if fallback_obj is not None:
        return fallback_obj

    raise ValueError("Çıktı içinde geçerli JSON bulunamadı")


def _unwrap_payload(obj: Any, _depth: int = 0) -> dict[str, Any]:
    """CLI veya HTTP zarf yapılarından iç `RouteDecision` sözlüğünü çıkarır."""
    if _depth > 6:
        raise ValueError("JSON zarf çözme derinliği aşıldı")

    if isinstance(obj, list):
        for item in reversed(obj):
            try:
                return _unwrap_payload(item, _depth + 1)
            except ValueError:
                continue
        raise ValueError("JSON dizisinde geçerli RouteDecision zarfı bulunamadı")

    if not isinstance(obj, dict):
        raise ValueError(f"Beklenmeyen JSON kökü: {type(obj).__name__}")

    if obj.get("is_error") is True:
        err_detail = str(obj.get("result") or obj.get("error") or "CLI hata bildirdi")
        raise ValueError(f"CLI hata zarfı: {err_detail[:240]}")

    if "is_research" in obj:
        return obj

    # CLI zarf alanları: structured_output, result, response, output, text, data
    for key in ("structured_output", "result", "response", "output", "text", "data"):
        val = obj.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, dict):
            try:
                return _unwrap_payload(val, _depth + 1)
            except ValueError:
                continue
        if isinstance(val, str):
            try:
                nested = _extract_json_value(val)
                return _unwrap_payload(nested, _depth + 1)
            except ValueError:
                continue

    # Gemini REST API zarfı: candidates[0].content.parts[*].text
    candidates = obj.get("candidates")
    if isinstance(candidates, list):
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            content = cand.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    try:
                        nested = _extract_json_value(part["text"])
                        return _unwrap_payload(nested, _depth + 1)
                    except ValueError:
                        continue

    # Anthropic REST API zarfı: content[*].input (tool_use) veya content[*].text
    content_blocks = obj.get("content")
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                try:
                    return _unwrap_payload(block["input"], _depth + 1)
                except ValueError:
                    continue
            if isinstance(block.get("text"), str):
                try:
                    nested = _extract_json_value(block["text"])
                    return _unwrap_payload(nested, _depth + 1)
                except ValueError:
                    continue

    raise ValueError("RouteDecision alanları JSON zarfında bulunamadı")


def parse_route_decision(raw_output: str, *, backend_used: str) -> RouteDecision:
    """Doğrudan JSON veya CLI/HTTP zarfından doğrulanmış `RouteDecision` üretir."""
    parsed = _extract_json_value(raw_output)
    payload = dict(_unwrap_payload(parsed))

    if payload.get("research_topic") is None:
        payload["research_topic"] = ""
    if payload.get("reason") is None:
        payload["reason"] = ""
    questions = payload.get("research_questions")
    if questions is None:
        payload["research_questions"] = []
    elif isinstance(questions, list) and len(questions) > 5:
        payload["research_questions"] = [str(q) for q in questions[:5]]

    payload["backend_used"] = backend_used
    try:
        return RouteDecision.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"RouteDecision şema doğrulama hatası: {exc}") from exc


class IntentRouter:
    """İki aşamalı (Stage 1 ön-filtre + Stage 2 LLM) Türkçe niyet yönlendiricisi."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg

    def decide_action(self, decision: RouteDecision) -> RouterAction:
        """Güven skoru ve niyet türüne göre aksiyon seçer (fail-closed)."""
        if not decision.is_research:
            return "note_only"
        if decision.intent_type in ("negated", "narrative_mention", "note_only"):
            return "note_only"
        if decision.confidence >= self.cfg.router.auto_start_threshold:
            return "auto_start"
        if decision.confidence >= self.cfg.router.confirm_threshold:
            return "confirm"
        return "note_only"

    def _build_backend_plan(self) -> list[str]:
        """Varsa doğrudan HTTP hızlı yollarını, ardından yapılandırılmış CLI backend'leri sıralar."""
        plan: list[str] = []
        if os.environ.get("GEMINI_API_KEY", "").strip():
            plan.append("gemini_http")
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            plan.append("anthropic_http")
        for backend in self.cfg.router.backends:
            if backend not in plan:
                plan.append(backend)
        return plan

    async def route(
        self, raw_transcript: str, *, force_stage2: bool = False
    ) -> tuple[RouteDecision, RouterAction]:
        """Transkripti yönlendirir: önce Stage 1 (<1 ms), eşleşirse Stage 2 sınıflandırma."""
        if not raw_transcript or not raw_transcript.strip():
            empty_decision = RouteDecision(
                is_research=False,
                confidence=0.0,
                intent_type="note_only",
                reason="no_match",
                backend_used="prefilter",
            )
            return empty_decision, "note_only"

        pre = stage1_prefilter(raw_transcript)
        if not pre.matched and not force_stage2:
            decision = RouteDecision(
                is_research=False,
                confidence=0.0,
                intent_type="note_only",
                reason=pre.reason,
                backend_used="prefilter",
            )
            return decision, "note_only"

        prompt = build_classifier_prompt(raw_transcript)
        schema_str = json.dumps(ROUTER_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        plan = self._build_backend_plan()

        errors: list[str] = []
        for backend in plan:
            try:
                decision = await self._invoke_backend(backend, prompt, schema_str)
                return decision, self.decide_action(decision)
            except Exception as exc:
                err_msg = f"{backend}: {exc}"
                errors.append(err_msg)
                logger.warning("Router backend başarısız (%s)", err_msg)

        raise RouterTransientError(
            "Tüm niyet sınıflandırıcı backend'leri başarısız oldu: "
            + " | ".join(errors or ["yapılandırılmış backend yok"])
        )

    async def _invoke_backend(
        self, backend: str, prompt: str, schema_str: str
    ) -> RouteDecision:
        if backend == "agy":
            return await self._run_agy(prompt, schema_str)
        if backend == "claude":
            return await self._run_claude(prompt, schema_str)
        if backend == "codex":
            return await self._run_codex(prompt, schema_str)
        if backend == "gemini_http":
            return await self._run_gemini_http(prompt)
        if backend == "anthropic_http":
            return await self._run_anthropic_http(prompt)
        raise ValueError(f"Bilinmeyen router backend: {backend}")

    async def _run_subprocess(
        self,
        cmd: list[str],
        *,
        stdin_text: str | None = None,
    ) -> tuple[int, str, str]:
        timeout = self.cfg.router.timeout_secs
        stdin_mode = (
            asyncio.subprocess.PIPE
            if stdin_text is not None
            else asyncio.subprocess.DEVNULL
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=timeout,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            raise RouterTransientError(
                f"{cmd[0]} zaman aşımı ({timeout:.1f}s)"
            ) from exc

        returncode = proc.returncode if proc.returncode is not None else 1
        return (
            returncode,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    async def _run_agy(self, prompt: str, schema_str: str) -> RouteDecision:
        cmd = build_agy_command(self.cfg.router.agy_model, schema_str, prompt)
        rc, stdout, stderr = await self._run_subprocess(cmd, stdin_text=None)
        if rc != 0:
            detail = (stderr or stdout).strip()[:240]
            if _looks_transient(detail):
                raise RouterTransientError(f"geçici hata (rc={rc}): {detail}")
            raise RuntimeError(f"rc={rc}: {detail}")
        return parse_route_decision(stdout, backend_used="agy")

    async def _run_claude(self, prompt: str, schema_str: str) -> RouteDecision:
        cmd = build_claude_command(self.cfg.router.claude_model, schema_str, prompt)
        rc, stdout, stderr = await self._run_subprocess(cmd, stdin_text=None)
        if rc != 0:
            detail = (stderr or stdout).strip()[:240]
            if _looks_transient(detail):
                raise RouterTransientError(f"geçici hata (rc={rc}): {detail}")
            raise RuntimeError(f"rc={rc}: {detail}")
        return parse_route_decision(stdout, backend_used="claude")

    async def _run_codex(self, prompt: str, schema_str: str) -> RouteDecision:
        with tempfile.TemporaryDirectory(prefix="seslinot-router-codex-") as tmpdir:
            tmp = Path(tmpdir)
            schema_file = tmp / "router_schema.json"
            out_file = tmp / "router_out.json"
            schema_file.write_text(schema_str, encoding="utf-8")

            cmd = build_codex_command(
                self.cfg.router.codex_model, schema_file, out_file
            )
            # Prompt stdin üzerinden iletilir (</dev/null YOK)
            rc, stdout, stderr = await self._run_subprocess(cmd, stdin_text=prompt)
            if rc != 0:
                detail = (stderr or stdout).strip()[:240]
                if _looks_transient(detail):
                    raise RouterTransientError(f"geçici hata (rc={rc}): {detail}")
                raise RuntimeError(f"rc={rc}: {detail}")

            raw_output = ""
            if out_file.is_file():
                raw_output = out_file.read_text(encoding="utf-8", errors="replace").strip()
            if not raw_output:
                raw_output = stdout
            return parse_route_decision(raw_output, backend_used="codex")

    async def _run_gemini_http(self, prompt: str) -> RouteDecision:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY tanımlı değil")

        model = os.environ.get("SESLINOT_GEMINI_HTTP_MODEL", "").strip()
        if not model:
            model = (
                self.cfg.router.agy_model
                if self.cfg.router.agy_model.startswith("gemini")
                else "gemini-2.5-flash"
            )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": CLASSIFIER_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseJsonSchema": ROUTER_JSON_SCHEMA,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.cfg.router.timeout_secs) as client:
                resp = await client.post(
                    url,
                    headers={
                        "x-goog-api-key": api_key,
                        "content-type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise RouterTransientError(f"HTTP bağlantı hatası: {type(exc).__name__}") from exc

        if resp.status_code in (429, 500, 502, 503, 504):
            raise RouterTransientError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

        return parse_route_decision(resp.text, backend_used="gemini_http")

    async def _run_anthropic_http(self, prompt: str) -> RouteDecision:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil")

        configured = self.cfg.router.claude_model.strip()
        model_id = _ANTHROPIC_MODEL_ALIASES.get(configured, configured)
        url = "https://api.anthropic.com/v1/messages"
        body = {
            "model": model_id,
            "max_tokens": 512,
            "temperature": 0.0,
            "system": CLASSIFIER_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": "classify_intent",
                    "description": "Türkçe sesli notun araştırma niyetini JSON olarak sınıflandır.",
                    "input_schema": ROUTER_JSON_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "classify_intent"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.cfg.router.timeout_secs) as client:
                resp = await client.post(
                    url,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise RouterTransientError(f"HTTP bağlantı hatası: {type(exc).__name__}") from exc

        if resp.status_code in (429, 500, 502, 503, 504, 529):
            raise RouterTransientError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

        return parse_route_decision(resp.text, backend_used="anthropic_http")
