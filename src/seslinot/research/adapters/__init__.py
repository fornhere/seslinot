"""CLI worker adaptör paketi ve kayıt defteri dışa aktarımları."""

from __future__ import annotations

from seslinot.research.adapters.agy import AgyAdapter
from seslinot.research.adapters.base import (
    WorkerAdapter,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
    ensure_strict_json_schema,
    extract_json_from_text,
    extract_snippets_from_stream,
    extract_urls_from_stream,
    get_adapter,
    register_adapter,
)
from seslinot.research.adapters.claude import ClaudeAdapter
from seslinot.research.adapters.codex import CodexAdapter
from seslinot.research.adapters.opencode import OpenCodeAdapter

__all__ = [
    "AgyAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "OpenCodeAdapter",
    "WorkerAdapter",
    "WorkerRequest",
    "WorkerResult",
    "WorkerRole",
    "ensure_strict_json_schema",
    "extract_json_from_text",
    "extract_snippets_from_stream",
    "extract_urls_from_stream",
    "get_adapter",
    "register_adapter",
]
