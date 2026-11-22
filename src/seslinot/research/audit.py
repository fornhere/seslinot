"""Çapraz şirket bağımsız atıf denetimi ve fail-closed onarım kapısı."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from seslinot.config import SesliNotConfig
from seslinot.models import (
    ClaimAuditItem,
    QuoteVerification,
    SourceRecord,
    VerificationReport,
)
from seslinot.research.adapters.base import (
    WorkerAdapter,
    WorkerRequest,
    get_adapter,
)
from seslinot.research.runner import ProcessRunner
from seslinot.research.verifier import audit_draft_citations, strip_llm_bibliography

_CITATION_ID_RE = re.compile(r"\[\^([SV]\d+)\]")
_SENTENCE_SPLIT_RE = re.compile(r"([.!?]+(?:[\"')\]]|\s*\[\^[A-Za-z0-9_-]+\])*)\s+")
_RAW_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`]+", re.IGNORECASE)

_BACKEND_COMPANY: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "agy": "google",
    "opencode": "opencode",
}

_VALID_QUOTE_STATUSES = frozenset({"exact_match", "near_match", "snippet_only"})

RepairCallback = Callable[[str, dict[str, Any]], Awaitable[str]]


class AuditResponse(BaseModel):
    """Denetçi ajanın zorunlu JSON yanıt şeması."""

    items: list[ClaimAuditItem] = Field(default_factory=list)


def _split_block_sentences(block: str) -> list[str]:
    """Paragrafı atıf etiketlerini ([^S01]) cümlenin sonuna bağlı tutarak cümlelere böler."""
    text = block.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    sentences: list[str] = []
    idx = 0
    while idx < len(parts):
        chunk = parts[idx]
        if idx + 1 < len(parts):
            chunk = chunk + parts[idx + 1]
            idx += 2
        else:
            idx += 1
        cleaned = chunk.strip()
        if cleaned:
            sentences.append(cleaned)
    return sentences


def extract_claim_triples(
    draft_md: str,
    quote_checks: list[QuoteVerification],
    registry: Mapping[str, SourceRecord],
    *,
    max_claims: int = 30,
    max_quote_chars: int = 280,
    max_total_chars: int = 12000,
) -> list[dict[str, Any]]:
    """Taslak metninden kompakt (türkçe_cümle, source_ids, exact_quotes) üçlüleri çıkarır."""
    body = strip_llm_bibliography(draft_md)

    quotes_by_source: dict[str, list[str]] = {}
    for qc in quote_checks:
        if qc.quote_status not in _VALID_QUOTE_STATUSES or not qc.source_id:
            continue
        q = qc.exact_quote.strip()
        if not q:
            continue
        if len(q) > max_quote_chars:
            q = q[: max_quote_chars - 1].rstrip() + "…"
        bucket = quotes_by_source.setdefault(qc.source_id, [])
        if q not in bucket and len(bucket) < 3:
            bucket.append(q)

    triples: list[dict[str, Any]] = []
    seen_sentences: set[str] = set()
    used_chars = 0

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        # Madde işareti öneki varsa temizle
        line_clean = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line)
        for sent in _split_block_sentences(line_clean):
            sids = list(dict.fromkeys(_CITATION_ID_RE.findall(sent)))
            if not sids:
                continue
            if sent in seen_sentences:
                continue
            seen_sentences.add(sent)

            matched_quotes: dict[str, list[str]] = {}
            for sid in sids:
                if sid in quotes_by_source:
                    matched_quotes[sid] = quotes_by_source[sid]
                elif sid in registry and registry[sid].title:
                    matched_quotes[sid] = [f"(Başlık: {registry[sid].title})"]

            triple = {
                "turkce_cumle": sent,
                "source_ids": sids,
                "exact_quotes": matched_quotes,
            }
            est_len = len(json.dumps(triple, ensure_ascii=False))
            if triples and (used_chars + est_len > max_total_chars or len(triples) >= max_claims):
                return triples
            triples.append(triple)
            used_chars += est_len

    return triples


def apply_fail_closed_markers(
    draft_md: str,
    audit_items: list[ClaimAuditItem],
    ghost_citations: list[str],
    leaked_urls: list[str],
) -> str:
    """Onarım başarısız olursa çelişkili iddiaları [Doğrulanamadı] ile işaretler, hayalet atıf/URL temizler."""
    updated = draft_md

    for ghost in ghost_citations:
        updated = updated.replace(ghost, "")

    for url in leaked_urls:
        updated = updated.replace(url, "[bağlantı kaldırıldı]")
    # Kalan olası ham URL'leri de fail-closed temizle (## Kaynaklar hariç gövdede)
    body = strip_llm_bibliography(updated)
    body = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", body)
    body = _RAW_URL_RE.sub("[bağlantı kaldırıldı]", body)

    for item in audit_items:
        if item.verdict != "CONTRADICTED":
            continue
        target = item.sentence.strip()
        if not target:
            continue
        if target in body and "[Doğrulanamadı]" not in target:
            body = body.replace(target, f"{target} [Doğrulanamadı]", 1)
        else:
            # Hayalet atıf temizliği sonrası cümle metni hafif değişmiş olabilir
            plain_target = _CITATION_ID_RE.sub("", target).strip()
            if plain_target and plain_target in body and f"{plain_target} [Doğrulanamadı]" not in body:
                body = body.replace(plain_target, f"{plain_target} [Doğrulanamadı]", 1)

    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r" +([.,;:!?])", r"\1", body)
    return body.strip()


def build_verification_report(
    registry: Mapping[str, SourceRecord],
    quote_checks: list[QuoteVerification],
    ghost_citations: list[str],
    leaked_urls: list[str],
    audit_status: str,
    audit_items: list[ClaimAuditItem],
) -> VerificationReport:
    """Kayıt defteri, alıntı doğrulaması ve denetim sonuçlarından VerificationReport derler."""
    records = list(registry.values())
    verified_sources = sum(
        1
        for r in records
        if r.fetch_status in ("fetched_ok", "fetch_blocked_snippet_only", "vault_ok")
    )
    dead_sources = sum(1 for r in records if r.fetch_status in ("dead_url", "vault_missing"))

    return VerificationReport(
        total_sources=len(records),
        verified_sources=verified_sources,
        dead_sources=dead_sources,
        quote_exact_match=sum(1 for q in quote_checks if q.quote_status == "exact_match"),
        quote_near_match=sum(1 for q in quote_checks if q.quote_status == "near_match"),
        quote_snippet_only=sum(1 for q in quote_checks if q.quote_status == "snippet_only"),
        quote_ungrounded=sum(1 for q in quote_checks if q.quote_status == "ungrounded_quote"),
        ghost_citations=list(ghost_citations),
        leaked_urls=list(leaked_urls),
        audit_status=audit_status,  # type: ignore[arg-type]
        quote_checks=list(quote_checks),
        audit_items=list(audit_items),
    )


class CitationAuditor:
    """Sentezleyiciyle farklı şirket modelini kullanarak salt-okunur atıf denetimi yapar."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        adapter_factory: Callable[[str], WorkerAdapter] = get_adapter,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.cfg = cfg
        self.adapter_factory = adapter_factory
        self.runner = runner or ProcessRunner(use_systemd_run=cfg.research.use_systemd_run)

    def select_audit_backends(self, synth_backend: str) -> list[tuple[str, str]]:
        """Sentezleyiciden farklı şirket denetçisini birincil, diğerini yedek olarak seçer."""
        synth_key = synth_backend.strip().lower()
        synth_company = _BACKEND_COMPANY.get(synth_key, synth_key)

        primary_b = self.cfg.research.audit_backend.strip().lower()
        primary_m = self.cfg.research.audit_model
        fallback_b = self.cfg.research.audit_fallback_backend.strip().lower()
        fallback_m = self.cfg.research.audit_fallback_model

        candidates: list[tuple[str, str]] = []
        if _BACKEND_COMPANY.get(primary_b, primary_b) != synth_company:
            candidates.append((primary_b, primary_m))
            candidates.append((fallback_b, fallback_m))
        else:
            # Sentezleyici ile aynı şirket denk gelirse çapraz şirketi öne al
            if _BACKEND_COMPANY.get(fallback_b, fallback_b) != synth_company:
                candidates.append((fallback_b, fallback_m))
                candidates.append((primary_b, primary_m))
            else:
                cross = ("codex", primary_m) if synth_key == "claude" else ("claude", fallback_m)
                candidates.append(cross)
                candidates.append((primary_b, primary_m))

        deduped: list[tuple[str, str]] = []
        seen_backends: set[str] = set()
        for b, m in candidates:
            if b and b not in seen_backends:
                seen_backends.add(b)
                deduped.append((b, m))
        return deduped

    def _build_audit_prompt(self, triples: list[dict[str, Any]]) -> tuple[str, str]:
        system_prompt = (
            "Sen bağımsız ve katı bir akademik atıf denetçisisin. "
            "Sana verilen her Türkçe cümleyi YALNIZCA yanındaki doğrulanmış kaynak alıntılarıyla "
            "(exact_quotes) karşılaştır. Web araması veya dış bilgi kullanma.\n"
            "Karar etiketleri:\n"
            "- SUPPORTED: Cümle alıntılarla tam uyumlu.\n"
            "- OVERSTATED: Alıntıdaki kapsam abartılmış veya genelleştirilmiş.\n"
            "- CONTRADICTED: Cümle kaynak alıntısıyla açıkça çelişiyor veya rakam/olgu ters.\n"
            "- UNSUPPORTED: Verilen alıntılar bu iddiayı desteklemiyor.\n"
            "Yalnızca istenen JSON şemasında yanıt ver."
        )
        payload = json.dumps({"claims": triples}, ensure_ascii=False, indent=2)
        user_prompt = (
            "Aşağıdaki Türkçe cümleleri yalnızca eşleşen doğrulanmış alıntılara göre denetle:\n\n"
            f"{payload}\n"
        )
        return system_prompt, user_prompt

    async def audit_draft(
        self,
        draft_md: str,
        quote_checks: list[QuoteVerification],
        registry: Mapping[str, SourceRecord],
        work_dir: Path,
        *,
        synth_backend: str,
        job_id: str = "",
    ) -> tuple[list[ClaimAuditItem], str, bool]:
        """Taslağı çapraz şirket denetçisiyle denetler. (items, backend_used, succeeded) döndürür."""
        triples = extract_claim_triples(draft_md, quote_checks, registry)
        if not triples:
            return [], "none", True

        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "claim_triples.json").write_text(
            json.dumps(triples, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        system_prompt, user_prompt = self._build_audit_prompt(triples)
        schema = AuditResponse.model_json_schema()
        backends = self.select_audit_backends(synth_backend)

        for idx, (backend_name, model_name) in enumerate(backends):
            run_dir = work_dir if idx == 0 else work_dir / f"fallback_{backend_name}"
            run_dir.mkdir(parents=True, exist_ok=True)
            adapter = self.adapter_factory(backend_name)
            unit = f"seslinot-audit-{job_id}-{idx}" if job_id else None

            req = WorkerRequest(
                worker_id=f"auditor_{backend_name}",
                role="auditor",
                prompt=user_prompt,
                system_prompt=system_prompt,
                work_dir=run_dir,
                json_schema=schema,
                model=model_name,
                reasoning_effort="medium",
                allow_web=False,
                timeout_secs=min(180, self.cfg.research.worker_timeout_secs),
                max_budget_usd=min(0.50, self.cfg.research.max_budget_usd),
                unit_name=unit,
            )
            res = await self.runner.run(adapter, req)
            if not res.ok or not res.parsed_json:
                continue
            try:
                parsed = AuditResponse.model_validate(res.parsed_json)
            except Exception:
                continue

            (work_dir / "audit.json").write_text(
                parsed.model_dump_json(indent=2),
                encoding="utf-8",
            )
            return parsed.items, backend_name, True

        return [], "failed", False

    async def audit_and_repair(
        self,
        draft_md: str,
        quote_checks: list[QuoteVerification],
        registry: Mapping[str, SourceRecord],
        audit_dir: Path,
        *,
        synth_backend: str,
        repair_fn: RepairCallback | None = None,
        job_id: str = "",
    ) -> tuple[str, VerificationReport]:
        """Deterministik + çapraz şirket denetimini uygular, gerekirse 1 tur onarır veya fail-closed kapatır."""
        current_draft = draft_md
        ghosts, leaked = audit_draft_citations(current_draft, registry)

        triples = extract_claim_triples(current_draft, quote_checks, registry)
        if not triples and not ghosts and not leaked:
            report = build_verification_report(
                registry, quote_checks, ghosts, leaked, "skipped", []
            )
            return current_draft, report

        audit_items, _backend, audit_ok = await self.audit_draft(
            current_draft,
            quote_checks,
            registry,
            audit_dir,
            synth_backend=synth_backend,
            job_id=job_id,
        )

        contradicted = [it for it in audit_items if it.verdict == "CONTRADICTED"]
        needs_repair = bool(contradicted or ghosts or leaked)

        if not needs_repair:
            status = "passed" if audit_ok else "skipped"
            report = build_verification_report(
                registry, quote_checks, ghosts, leaked, status, audit_items
            )
            return current_draft, report

        repaired_cleanly = False
        max_rounds = max(0, int(self.cfg.research.repair_max_rounds))

        if repair_fn is not None and max_rounds > 0:
            for round_num in range(1, max_rounds + 1):
                issues = {
                    "round": round_num,
                    "ghost_citations": ghosts,
                    "leaked_urls": leaked,
                    "contradicted_claims": [it.model_dump() for it in contradicted],
                    "overstated_claims": [
                        it.model_dump() for it in audit_items if it.verdict == "OVERSTATED"
                    ],
                }
                candidate_draft = await repair_fn(current_draft, issues)
                if candidate_draft and candidate_draft.strip():
                    current_draft = candidate_draft

                ghosts, leaked = audit_draft_citations(current_draft, registry)
                audit_items, _backend, audit_ok = await self.audit_draft(
                    current_draft,
                    quote_checks,
                    registry,
                    audit_dir / f"round_{round_num}",
                    synth_backend=synth_backend,
                    job_id=f"{job_id}-r{round_num}" if job_id else "",
                )
                contradicted = [it for it in audit_items if it.verdict == "CONTRADICTED"]
                if not contradicted and not ghosts and not leaked:
                    repaired_cleanly = True
                    break

        if repaired_cleanly:
            report = build_verification_report(
                registry, quote_checks, ghosts, leaked, "repaired", audit_items
            )
            return current_draft, report

        # Fail-closed: onarım tamamlanmadıysa veya sorun kaldıysa tehlikeli atıf/URL/çelişkiyi kapat
        gated_draft = apply_fail_closed_markers(current_draft, audit_items, ghosts, leaked)
        final_ghosts, final_leaked = audit_draft_citations(gated_draft, registry)
        report = build_verification_report(
            registry, quote_checks, final_ghosts, final_leaked, "failed_closed", audit_items
        )
        return gated_draft, report
