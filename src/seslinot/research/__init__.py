"""Çok-ajanlı derin araştırma motoru ve deterministik atıf doğrulama paketi."""

from __future__ import annotations

from seslinot.research.adapters import (
    AgyAdapter,
    ClaudeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    WorkerAdapter,
    WorkerRequest,
    WorkerResult,
    extract_json_from_text,
    extract_snippets_from_stream,
    extract_urls_from_stream,
    get_adapter,
    register_adapter,
)
from seslinot.research.audit import (
    AuditResponse,
    CitationAuditor,
    apply_fail_closed_markers,
    extract_claim_triples,
)
from seslinot.research.registry import (
    FetchResponse,
    SourceRegistry,
    canonicalize_url,
    extract_clean_text,
    is_ssrf_safe_url,
)
from seslinot.research.runner import ProcessRunner, cancel_unit, sanitize_unit_name
from seslinot.research.state_machine import ResearchOrchestrator, sanitize_web_queries
from seslinot.research.verifier import (
    audit_draft_citations,
    normalize_for_match,
    render_deterministic_report,
    strip_llm_bibliography,
    verify_all_findings,
    verify_quote_against_snapshot,
)

__all__ = [
    "AgyAdapter",
    "AuditResponse",
    "CitationAuditor",
    "ClaudeAdapter",
    "CodexAdapter",
    "FetchResponse",
    "OpenCodeAdapter",
    "ProcessRunner",
    "ResearchOrchestrator",
    "SourceRegistry",
    "WorkerAdapter",
    "WorkerRequest",
    "WorkerResult",
    "apply_fail_closed_markers",
    "audit_draft_citations",
    "cancel_unit",
    "canonicalize_url",
    "extract_claim_triples",
    "extract_clean_text",
    "extract_json_from_text",
    "extract_snippets_from_stream",
    "extract_urls_from_stream",
    "get_adapter",
    "is_ssrf_safe_url",
    "normalize_for_match",
    "register_adapter",
    "render_deterministic_report",
    "sanitize_unit_name",
    "sanitize_web_queries",
    "strip_llm_bibliography",
    "verify_all_findings",
    "verify_quote_against_snapshot",
]
