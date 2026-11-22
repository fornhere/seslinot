"""Araştırma worker CLI adaptörleri için ortak sözleşme ve yardımcı fonksiyonlar."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

WorkerRole = Literal["lead", "subagent", "synthesizer", "auditor"]

_URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\}]+", re.IGNORECASE)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)

_TOOL_NODE_TYPES = frozenset(
    {
        "tool_use",
        "tool_result",
        "web_search",
        "web_search_call",
        "web_fetch",
        "function_call",
        "function_call_output",
        "mcp_tool_call",
        "tool_call",
        "tool_output",
        "search_result",
        "citation",
        "url_citation",
    }
)

_TOOL_CONTAINER_KEYS = frozenset(
    {
        "tool_use_result",
        "tool",
        "tool_name",
        "web_search",
        "search_results",
        "groundingMetadata",
        "groundingChunks",
        "citations",
        "url_citations",
        "sources",
        "tool_calls",
        "functionCall",
        "functionResponse",
    }
)

_PURE_ASSISTANT_TYPES = frozenset(
    {
        "text",
        "thinking",
        "agent_message",
        "reasoning",
        "result",
    }
)


class WorkerRequest(BaseModel):
    """Tek bir izole CLI worker çağrısı için giriş sözleşmesi."""

    worker_id: str
    role: WorkerRole
    prompt: str
    system_prompt: str = ""
    work_dir: Path
    json_schema: dict[str, Any] | None = None
    model: str = ""
    reasoning_effort: str = "medium"
    allow_web: bool = False
    timeout_secs: int = 420
    max_budget_usd: float = 1.50
    unit_name: str | None = None


class WorkerResult(BaseModel):
    """Bir CLI worker çalıştırmasının normalize edilmiş sonucu."""

    ok: bool
    exit_code: int = 0
    output_text: str = ""
    parsed_json: dict[str, Any] | None = None
    raw_stream_path: Path | None = None
    stderr_path: Path | None = None
    observed_urls: list[str] = Field(default_factory=list)
    backend: str = ""
    error: str = ""


@runtime_checkable
class WorkerAdapter(Protocol):
    """Yerel CLI ajan backend'leri için ortak adaptör protokolü."""

    backend_name: str

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        """Komut satırını, opsiyonel stdin baytlarını ve opsiyonel çıktı dosya yolunu döndürür."""
        ...

    def parse_output(
        self,
        req: WorkerRequest,
        stdout_path: Path,
        output_file_path: Path | None,
    ) -> WorkerResult:
        """Ham stdout akışı ve çıktı dosyasından WorkerResult üretir."""
        ...


def _clean_extracted_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)\"'`>")


def extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Serbest metin veya Markdown kod bloğu içinden ilk/son geçerli JSON nesnesini çıkarır."""
    if not text or not text.strip():
        return None

    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Önce ```json ... ``` veya ``` ... ``` bloklarını sondan başa tara
    blocks = _FENCED_JSON_RE.findall(stripped)
    for block in reversed(blocks):
        candidate = block.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Dengeli süslü parantez tarayıcısı (dizge literallerini ve kaçışları dikkate alır)
    candidates: list[dict[str, Any]] = []
    n = len(stripped)
    idx = 0
    while idx < n:
        if stripped[idx] != "{":
            idx += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        end_idx = -1
        for pos in range(idx, n):
            ch = stripped[pos]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_idx = pos
                        break
        if end_idx != -1:
            snippet = stripped[idx : end_idx + 1]
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
                    idx = end_idx + 1
                    continue
            except json.JSONDecodeError:
                pass
        idx += 1

    if candidates:
        return candidates[-1]
    return None


def _collect_urls_from_any(obj: Any, out: list[str], seen: set[str]) -> None:
    if isinstance(obj, str):
        for match in _URL_RE.findall(obj):
            cleaned = _clean_extracted_url(match)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    elif isinstance(obj, list):
        for item in obj:
            _collect_urls_from_any(item, out, seen)
    elif isinstance(obj, dict):
        for val in obj.values():
            _collect_urls_from_any(val, out, seen)


def _walk_tool_nodes(node: Any, in_tool_context: bool, out: list[str], seen: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_tool_nodes(item, in_tool_context, out, seen)
        return

    if not isinstance(node, dict):
        if in_tool_context:
            _collect_urls_from_any(node, out, seen)
        return

    node_type = str(node.get("type", "")).lower()
    is_tool_node = in_tool_context or (node_type in _TOOL_NODE_TYPES)

    if not is_tool_node:
        for key in _TOOL_CONTAINER_KEYS:
            if key in node and node[key] is not None:
                # Yalnızca araç alt ağaçlarını araç bağlamında tara
                _walk_tool_nodes(node[key], True, out, seen)
                # Eğer düğümün kendisi bir araç kaydı ise (ör. {"tool": "WebFetch", "url": "..."})
                if key in {"tool", "tool_name", "web_search"}:
                    is_tool_node = True

    if is_tool_node:
        _collect_urls_from_any(node, out, seen)
        return

    # Saf asistan metin/sonuç bloklarında uydurma URL'leri tool trace sayma
    if node_type in _PURE_ASSISTANT_TYPES:
        for citation_key in ("citations", "url_citations", "groundingMetadata", "sources"):
            if citation_key in node:
                _walk_tool_nodes(node[citation_key], True, out, seen)
        return

    # Konteyner olayları (assistant/user message, item.completed, part vb.) içine in
    for key, val in node.items():
        if key in _TOOL_CONTAINER_KEYS:
            continue  # yukarıda işlendi
        if isinstance(val, (dict, list)):
            _walk_tool_nodes(val, False, out, seen)


def extract_urls_from_stream(stream_path: Path) -> list[str]:
    """raw_stream.jsonl içindeki araç çağrıları ve araç sonuçlarında görülen URL'leri çıkarır."""
    if not stream_path or not stream_path.is_file():
        return []

    urls: list[str] = []
    seen: set[str] = set()
    try:
        raw_text = stream_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        _walk_tool_nodes(event, False, urls, seen)

    return urls


def _collect_snippets_from_tool_node(node: Any, out: dict[str, list[str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_snippets_from_tool_node(item, out)
        return

    if not isinstance(node, dict):
        return

    direct_urls: list[str] = []
    for url_key in ("url", "uri", "href", "link", "source_url"):
        val = node.get(url_key)
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            direct_urls.append(_clean_extracted_url(val))

    text_chunks: list[str] = []
    for text_key in ("snippet", "excerpt", "content", "text", "output", "body", "summary"):
        val = node.get(text_key)
        if isinstance(val, str) and val.strip():
            text_chunks.append(val.strip())

    if direct_urls and text_chunks:
        combined = "\n".join(text_chunks)
        for u in direct_urls:
            out.setdefault(u, []).append(combined)
    elif text_chunks:
        for chunk in text_chunks:
            found = [_clean_extracted_url(m) for m in _URL_RE.findall(chunk)]
            for u in found:
                out.setdefault(u, []).append(chunk)

    for val in node.values():
        if isinstance(val, (dict, list)):
            _collect_snippets_from_tool_node(val, out)


def _walk_snippets(node: Any, in_tool_context: bool, out: dict[str, list[str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_snippets(item, in_tool_context, out)
        return
    if not isinstance(node, dict):
        return

    node_type = str(node.get("type", "")).lower()
    is_tool_node = in_tool_context or (node_type in _TOOL_NODE_TYPES)
    if not is_tool_node:
        for key in _TOOL_CONTAINER_KEYS:
            if key in node and node[key] is not None:
                _collect_snippets_from_tool_node(node[key], out)
                if key in {"tool", "tool_name", "web_search"}:
                    is_tool_node = True

    if is_tool_node:
        _collect_snippets_from_tool_node(node, out)
        return

    if node_type in _PURE_ASSISTANT_TYPES:
        return

    for key, val in node.items():
        if key in _TOOL_CONTAINER_KEYS:
            continue
        if isinstance(val, (dict, list)):
            _walk_snippets(val, False, out)


def extract_snippets_from_stream(stream_path: Path) -> dict[str, list[str]]:
    """raw_stream.jsonl içindeki araç sonuçlarından URL -> metin özetleri haritası çıkarır."""
    if not stream_path or not stream_path.is_file():
        return {}

    snippets: dict[str, list[str]] = {}
    try:
        raw_text = stream_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        _walk_snippets(event, False, snippets)

    return snippets


def ensure_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI/Codex --output-schema uyumluluğu için tüm nesnelere additionalProperties: false ekler."""
    strict = copy.deepcopy(schema)

    def _visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _visit(item)
            return
        if not isinstance(node, dict):
            return

        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            props = node.get("properties")
            if isinstance(props, dict):
                node["required"] = list(props.keys())
                for prop_schema in props.values():
                    _visit(prop_schema)

        for key in ("items", "additionalProperties"):
            val = node.get(key)
            if isinstance(val, dict):
                _visit(val)

        for key in ("anyOf", "oneOf", "allOf"):
            val = node.get(key)
            if isinstance(val, list):
                _visit(val)

        for defs_key in ("$defs", "definitions"):
            defs = node.get(defs_key)
            if isinstance(defs, dict):
                for sub in defs.values():
                    _visit(sub)

    _visit(strict)
    return strict


_ADAPTER_REGISTRY: dict[str, WorkerAdapter] = {}
_BUILTINS_LOADED = False


def register_adapter(adapter: WorkerAdapter) -> None:
    """Adaptörü adına göre merkezi kayıt defterine ekler."""
    _ADAPTER_REGISTRY[adapter.backend_name.strip().lower()] = adapter


def _ensure_builtin_adapters() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True

    from seslinot.research.adapters.agy import AgyAdapter
    from seslinot.research.adapters.claude import ClaudeAdapter
    from seslinot.research.adapters.codex import CodexAdapter
    from seslinot.research.adapters.opencode import OpenCodeAdapter

    for cls in (ClaudeAdapter, CodexAdapter, AgyAdapter, OpenCodeAdapter):
        inst = cls()
        _ADAPTER_REGISTRY.setdefault(inst.backend_name.lower(), inst)


def get_adapter(name: str) -> WorkerAdapter:
    """Backend adına göre ilgili WorkerAdapter örneğini döndürür."""
    _ensure_builtin_adapters()
    key = name.strip().lower()
    if key not in _ADAPTER_REGISTRY:
        available = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise ValueError(f"Bilinmeyen worker adapter: {name!r} (mevcut: {available})")
    return _ADAPTER_REGISTRY[key]
