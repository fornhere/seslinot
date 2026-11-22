"""0-token Span.verify alıntı doğrulayıcısı, hayalet atıf kapısı ve deterministik kaynakça."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Mapping

from rapidfuzz import fuzz

from seslinot.models import QuoteVerification, SourceRecord, SubagentOutput
from seslinot.research.registry import SourceRegistry, canonicalize_url

_SMART_TRANS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u200b": "",
    }
)

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_INLINE_RE = re.compile(r"(?:`+|\*\*|__|~~|\*|(?<!\w)_(?!\w))")
_MD_LINE_PREFIX_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+)")
_WS_RE = re.compile(r"\s+")

_CITATION_TOKEN_RE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
_FOOTNOTE_DEF_LINE_RE = re.compile(r"(?m)^\[\^[A-Za-z0-9_-]+\]:.*(?:\n|$)")
_BIB_SECTION_RE = re.compile(
    r"(?im)^#{2,3}\s+(?:Kaynaklar|Kaynakça|Sources|References|Bibliography)\s*$.*?(?=^##\s+|\Z)",
    re.DOTALL,
)
_RAW_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`]+", re.IGNORECASE)

_NEAR_MATCH_MIN_LEN = 25
_NEAR_MATCH_THRESHOLD = 88.0


def normalize_for_match(text: str) -> str:
    """Metni Unicode NFKC, akıllı tırnak/tire, Markdown ve boşluk açısından normalize eder."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    norm = norm.translate(_SMART_TRANS)
    norm = _MD_LINK_RE.sub(r"\1", norm)
    norm = _MD_LINE_PREFIX_RE.sub("", norm)
    norm = _MD_INLINE_RE.sub("", norm)
    # Türkçe 'İ' -> 'i' dönüşümünde birleştirici nokta (\u0307) üretmemek için özel ön işlem
    norm = norm.replace("İ", "i").lower()
    norm = _WS_RE.sub(" ", norm).strip()
    return norm


def verify_quote_against_snapshot(quote: str, snapshot_text: str) -> tuple[str, float]:
    """Alıntıyı indirilen anlık görüntü metnine karşı 0-token deterministik doğrular."""
    norm_q = normalize_for_match(quote)
    norm_s = normalize_for_match(snapshot_text)
    if not norm_q or not norm_s:
        return "ungrounded_quote", 0.0

    if norm_q in norm_s:
        return "exact_match", 100.0

    score = float(fuzz.partial_ratio(norm_q, norm_s))
    if len(norm_q) >= _NEAR_MATCH_MIN_LEN and score >= _NEAR_MATCH_THRESHOLD:
        return "near_match", round(score, 2)

    return "ungrounded_quote", round(score, 2)


def _lookup_snippets(
    rec: SourceRecord,
    tool_snippets: Mapping[str, str | list[str]] | None,
) -> str:
    if not tool_snippets:
        return ""

    chunks: list[str] = []
    candidate_keys: set[str] = {rec.source_id}
    if rec.canonical_url:
        candidate_keys.add(rec.canonical_url)
    if rec.final_url:
        candidate_keys.add(rec.final_url)
    for raw_u in rec.original_urls:
        candidate_keys.add(raw_u)
        c_u = canonicalize_url(raw_u)
        if c_u:
            candidate_keys.add(c_u)

    for key, val in tool_snippets.items():
        canon_key = canonicalize_url(key) if key.startswith(("http://", "https://")) else key
        if key in candidate_keys or canon_key in candidate_keys:
            if isinstance(val, str):
                if val.strip():
                    chunks.append(val.strip())
            else:
                chunks.extend(item.strip() for item in val if isinstance(item, str) and item.strip())

    return "\n\n".join(chunks)


def _read_record_text(
    rec: SourceRecord,
    registry: SourceRegistry | Mapping[str, SourceRecord],
    vault_path: Path | None = None,
) -> str:
    if isinstance(registry, SourceRegistry):
        return registry.get_snapshot_text(rec.source_id)

    if rec.snapshot_path:
        snap = Path(rec.snapshot_path)
        if snap.is_file():
            try:
                return snap.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
    if rec.source_type == "vault" and rec.vault_path and vault_path is not None:
        candidate = (vault_path / rec.vault_path).resolve()
        try:
            if candidate.is_relative_to(vault_path.resolve()) and candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def verify_all_findings(
    outputs: list[SubagentOutput],
    registry: SourceRegistry | Mapping[str, SourceRecord],
    tool_snippets: Mapping[str, str | list[str]] | None = None,
    *,
    finding_source_map: Mapping[tuple[str, str], str] | None = None,
    vault_path: Path | None = None,
) -> list[QuoteVerification]:
    """Tüm subagent bulgularındaki alıntıları kaynak anlık görüntüleriyle doğrular."""
    records: Mapping[str, SourceRecord] = (
        registry.records if isinstance(registry, SourceRegistry) else registry
    )

    url_to_sid: dict[str, str] = {}
    vault_to_sid: dict[str, str] = {}
    for sid, rec in records.items():
        if rec.source_type == "vault" and rec.vault_path:
            vault_to_sid[rec.vault_path.strip().lstrip("/")] = sid
        else:
            if rec.canonical_url:
                url_to_sid[rec.canonical_url] = sid
            if rec.final_url:
                url_to_sid[rec.final_url] = sid
            for orig in rec.original_urls:
                url_to_sid[orig] = sid
                c_orig = canonicalize_url(orig)
                if c_orig:
                    url_to_sid[c_orig] = sid

    results: list[QuoteVerification] = []
    for out in outputs:
        for finding in out.findings:
            sid = ""
            if finding_source_map is not None:
                sid = finding_source_map.get((out.worker_id, finding.claim_id), "")
            if not sid:
                if finding.source_type == "vault" and finding.vault_path:
                    sid = vault_to_sid.get(finding.vault_path.strip().lstrip("/"), "")
                elif finding.url:
                    canon = canonicalize_url(finding.url)
                    sid = url_to_sid.get(canon) or url_to_sid.get(finding.url, "")

            rec = records.get(sid) if sid else None
            if rec is None or rec.fetch_status in ("dead_url", "vault_missing"):
                results.append(
                    QuoteVerification(
                        claim_id=finding.claim_id,
                        worker_id=out.worker_id,
                        source_id=sid,
                        quote_status="dead_source",
                        similarity=0.0,
                        claim_tr=finding.claim_tr,
                        exact_quote=finding.exact_quote,
                    )
                )
                continue

            if rec.fetch_status in ("fetched_ok", "vault_ok"):
                snap_text = _read_record_text(rec, registry, vault_path=vault_path)
                status, score = verify_quote_against_snapshot(finding.exact_quote, snap_text)
                results.append(
                    QuoteVerification(
                        claim_id=finding.claim_id,
                        worker_id=out.worker_id,
                        source_id=sid,
                        quote_status=status,  # type: ignore[arg-type]
                        similarity=score,
                        claim_tr=finding.claim_tr,
                        exact_quote=finding.exact_quote,
                    )
                )
                continue

            # fetch_blocked_snippet_only
            snippet_blob = _lookup_snippets(rec, tool_snippets)
            if snippet_blob:
                status, score = verify_quote_against_snapshot(finding.exact_quote, snippet_blob)
                if status in ("exact_match", "near_match"):
                    q_status = "snippet_only"
                else:
                    q_status = "ungrounded_quote"
                results.append(
                    QuoteVerification(
                        claim_id=finding.claim_id,
                        worker_id=out.worker_id,
                        source_id=sid,
                        quote_status=q_status,  # type: ignore[arg-type]
                        similarity=score,
                        claim_tr=finding.claim_tr,
                        exact_quote=finding.exact_quote,
                    )
                )
            elif rec.seen_in_tool_trace:
                results.append(
                    QuoteVerification(
                        claim_id=finding.claim_id,
                        worker_id=out.worker_id,
                        source_id=sid,
                        quote_status="snippet_only",
                        similarity=0.0,
                        claim_tr=finding.claim_tr,
                        exact_quote=finding.exact_quote,
                    )
                )
            else:
                results.append(
                    QuoteVerification(
                        claim_id=finding.claim_id,
                        worker_id=out.worker_id,
                        source_id=sid,
                        quote_status="dead_source",
                        similarity=0.0,
                        claim_tr=finding.claim_tr,
                        exact_quote=finding.exact_quote,
                    )
                )

    return results


def strip_llm_bibliography(draft_md: str) -> str:
    """LLM tarafından üretilmiş olası `## Kaynaklar` bölümünü ve dipnot tanımlarını temizler."""
    cleaned = _BIB_SECTION_RE.sub("", draft_md)
    cleaned = _FOOTNOTE_DEF_LINE_RE.sub("", cleaned)
    return cleaned.strip()


def audit_draft_citations(
    draft_md: str,
    registry: Mapping[str, SourceRecord],
) -> tuple[list[str], list[str]]:
    """Taslak gövdesindeki hayalet atıfları (kayıtsız/ölü kaynak) ve sızan ham URL'leri yakalar."""
    body = strip_llm_bibliography(draft_md)

    ghost_citations: list[str] = []
    seen_ghosts: set[str] = set()
    for match in _CITATION_TOKEN_RE.finditer(body):
        cid = match.group(1)
        rec = registry.get(cid)
        if rec is None or rec.fetch_status in ("dead_url", "vault_missing"):
            token = f"[^{cid}]"
            if token not in seen_ghosts:
                seen_ghosts.add(token)
                ghost_citations.append(token)

    leaked_urls: list[str] = []
    seen_urls: set[str] = set()
    for match in _RAW_URL_RE.finditer(body):
        u = match.group(0).rstrip(".,;:!?)\"'`>")
        if u and u not in seen_urls:
            seen_urls.add(u)
            leaked_urls.append(u)

    return ghost_citations, leaked_urls


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _format_vault_wikilink(rec: SourceRecord) -> str:
    rel = (rec.vault_path or "").strip().lstrip("/")
    rel_no_ext = Path(rel).with_suffix("").as_posix() if rel else rec.source_id
    title = _escape_md_cell(rec.title) if rec.title else ""
    if title and title != Path(rel_no_ext).name:
        return f"[[{rel_no_ext}|{title}]]"
    return f"[[{rel_no_ext}]]"


def render_deterministic_report(
    draft_md: str,
    registry: Mapping[str, SourceRecord],
    *,
    renumber: bool = True,
) -> tuple[str, list[SourceRecord]]:
    """Taslağı temizler, yalnızca doğrulanmış atıfları numaralandırır ve deterministik `## Kaynaklar` ekler."""
    body = strip_llm_bibliography(draft_md)

    cited_ids: list[str] = []
    seen_ids: set[str] = set()

    def _inspect_and_rewrite(match: re.Match[str]) -> str:
        cid = match.group(1)
        rec = registry.get(cid)
        if rec is None or rec.fetch_status in ("dead_url", "vault_missing"):
            # Fail-closed: hayalet veya ölü atıfı nihai raporda bırakma
            return ""
        if cid not in seen_ids:
            seen_ids.add(cid)
            cited_ids.append(cid)
        if renumber:
            num = cited_ids.index(cid) + 1
            return f"[^{num}]"
        return f"[^{cid}]"

    rewritten_body = _CITATION_TOKEN_RE.sub(_inspect_and_rewrite, body)
    # Hayalet atıf silinmesi sonrası oluşan çift boşlukları temizle (satır sonlarını koru)
    rewritten_body = re.sub(r"[ \t]{2,}", " ", rewritten_body)
    rewritten_body = re.sub(r" +([.,;:!?])", r"\1", rewritten_body).strip()

    cited_records = [registry[cid] for cid in cited_ids]

    bib_lines: list[str] = ["## Kaynaklar", ""]
    if not cited_records:
        bib_lines.append("_Doğrulanmış kaynak bulunamadı._")
    else:
        bib_lines.append("| # | Kaynak | Tür | Tarih | Durum |")
        bib_lines.append("|---|---|---|---|---|")
        footnote_lines: list[str] = []

        for idx, rec in enumerate(cited_records, start=1):
            label = str(idx) if renumber else rec.source_id
            date_str = (rec.published_at or rec.retrieved_at or "")[:10] or "-"

            if rec.source_type == "vault":
                link_md = _format_vault_wikilink(rec)
                kind_tr = "Hafıza Kasası"
                status_tr = "Doğrulandı"
                fn_text = f"[^{label}]: {link_md} — Hafıza Kasası (Erişim: {date_str})"
            else:
                url = rec.final_url or rec.canonical_url
                title = _escape_md_cell(rec.title) or url
                link_md = f"[{title}]({url})"
                kind_tr = "Web"
                if rec.fetch_status == "fetched_ok":
                    status_tr = "Doğrulandı"
                    fn_text = f"[^{label}]: [{title}]({url}) — Erişim: {date_str}"
                else:
                    status_tr = "Özet (Erişim Kısıtlı)"
                    fn_text = f"[^{label}]: [{title}]({url}) — Erişim: {date_str} (Özet doğrulamalı)"

            bib_lines.append(f"| {label} | {link_md} | {kind_tr} | {date_str} | {status_tr} |")
            footnote_lines.append(fn_text)

        bib_lines.append("")
        bib_lines.extend(footnote_lines)

    final_md = f"{rewritten_body}\n\n" + "\n".join(bib_lines) + "\n"
    return final_md, cited_records
