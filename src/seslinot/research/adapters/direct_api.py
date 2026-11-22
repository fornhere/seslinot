"""Direct HTTP API (OpenAI-uyumlu / Anthropic) + Web Arama araştırma adaptörü.

Claude Code / Codex CLI aboneliği olmayan Windows, macOS ve Linux kullanıcılarının
yalnızca bir API anahtarı (`OPENAI_API_KEY` veya `ANTHROPIC_API_KEY`) ile tam
4+1 fazlı çok-ajanlı derin araştırma çalıştırabilmesini sağlar.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from seslinot.research.adapters.base import (
    WorkerAdapter,
    WorkerRequest,
    WorkerResult,
    extract_json_from_text,
    register_adapter,
)

_DDG_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|'
    r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub(" ", raw))).strip()


def _unwrap_ddg_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href


def parse_ddg_html_results(raw_html: str, *, max_results: int = 6) -> list[dict[str, str]]:
    """DuckDuckGo HTML sayfasından başlık, URL ve özet parçalarını ayıklar."""
    links = _DDG_LINK_RE.findall(raw_html)
    snippets = [a or b for a, b in _DDG_SNIPPET_RE.findall(raw_html)]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for idx, (href, raw_title) in enumerate(links):
        url = _unwrap_ddg_url(html.unescape(href))
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        snippet = _strip_tags(snippets[idx]) if idx < len(snippets) else ""
        results.append(
            {
                "title": _strip_tags(raw_title),
                "url": url,
                "snippet": snippet,
            }
        )
        if len(results) >= max_results:
            break
    return results


def _extract_queries_from_prompt(prompt: str) -> list[str]:
    """Subagent promptundan arama sorgularını çıkarır."""
    queries: list[str] = []
    for line in prompt.splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")) and 6 <= len(s[2:].strip()) <= 160:
            candidate = s[2:].strip().strip("`\"'")
            if not candidate.lower().startswith(("http", "not:", "kural:", "yasak")):
                queries.append(candidate)
        if len(queries) >= 4:
            break
    if not queries:
        first_line = " ".join(prompt.strip().split())[:140]
        if first_line:
            queries.append(first_line)
    return queries[:3]


async def search_web_snippets(
    queries: list[str],
    *,
    provider: str = "auto",
    client: httpx.AsyncClient | None = None,
    max_per_query: int = 5,
) -> list[dict[str, str]]:
    """Tavily, Brave veya sıfır-anahtar DuckDuckGo HTML üzerinden web araması yapar."""
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()

    chosen = provider
    if chosen == "auto":
        if tavily_key:
            chosen = "tavily"
        elif brave_key:
            chosen = "brave"
        else:
            chosen = "ddg"

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    try:
        for q in queries:
            q_clean = q.strip()
            if not q_clean:
                continue
            batch: list[dict[str, str]] = []
            try:
                if chosen == "tavily" and tavily_key:
                    resp = await http.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": tavily_key,
                            "query": q_clean,
                            "max_results": max_per_query,
                            "search_depth": "basic",
                        },
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("results", []):
                            batch.append(
                                {
                                    "title": str(item.get("title") or ""),
                                    "url": str(item.get("url") or ""),
                                    "snippet": str(item.get("content") or ""),
                                }
                            )
                elif chosen == "brave" and brave_key:
                    resp = await http.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": q_clean, "count": max_per_query},
                        headers={"X-Subscription-Token": brave_key, "Accept": "application/json"},
                    )
                    if resp.status_code == 200:
                        web_items = resp.json().get("web", {}).get("results", [])
                        for item in web_items:
                            batch.append(
                                {
                                    "title": str(item.get("title") or ""),
                                    "url": str(item.get("url") or ""),
                                    "snippet": str(item.get("description") or ""),
                                }
                            )
                else:
                    resp = await http.post(
                        "https://html.duckduckgo.com/html/",
                        data={"q": q_clean},
                        headers={"User-Agent": "SesliNot/0.2 (+https://github.com/fornhere/seslinot)"},
                    )
                    if resp.status_code == 200:
                        batch = parse_ddg_html_results(resp.text, max_results=max_per_query)
            except (httpx.HTTPError, ValueError):
                batch = []

            for item in batch:
                url = item.get("url", "").strip()
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append(item)
    finally:
        if owns_client:
            await http.aclose()

    return results


class DirectApiAdapter(WorkerAdapter):
    """CLI kurulumu olmadan OpenAI-uyumlu veya Anthropic HTTP API üzerinden çalışan adaptör."""

    backend_name = "api"

    def __init__(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        default_model: str | None = None,
        search_provider: str = "auto",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = (provider or os.environ.get("SESLINOT_API_PROVIDER") or "openai").lower()
        self.base_url = base_url or os.environ.get("SESLINOT_API_BASE_URL") or ""
        self.api_key_env = api_key_env or os.environ.get("SESLINOT_API_KEY_ENV") or ""
        self.default_model = default_model or os.environ.get("SESLINOT_API_MODEL") or (
            "claude-sonnet-4-20250514" if self.provider == "anthropic" else "gpt-4o-mini"
        )
        self.search_provider = search_provider
        self._http_client = http_client

    def _resolve_api_key(self) -> str:
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip()
        if self.provider == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY", "").strip()
        return (
            os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )

    def build_command(
        self, req: WorkerRequest
    ) -> tuple[list[str], bytes | None, Path | None]:
        # ProcessRunner execute_direct() yolunu çağırır; arayüz uyumluluğu için korunur.
        return ["seslinot-direct-api"], None, None

    def parse_output(
        self,
        req: WorkerRequest,
        stdout_path: Path,
        output_file_path: Path | None,
    ) -> WorkerResult:
        raw = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        parsed = extract_json_from_text(raw) if req.json_schema is not None else None
        return WorkerResult(
            ok=bool(raw.strip()) and (parsed is not None if req.json_schema is not None else True),
            exit_code=0,
            output_text=raw,
            parsed_json=parsed,
            raw_stream_path=stdout_path,
            backend=self.backend_name,
        )

    async def execute_direct(self, req: WorkerRequest) -> WorkerResult:
        """HTTP LLM çağrısını ve gerekirse web aramasını doğrudan yürütür."""
        req.work_dir.mkdir(parents=True, exist_ok=True)
        raw_stream_path = req.work_dir / "raw_stream.jsonl"
        stderr_path = req.work_dir / "worker.log"
        exit_code_path = req.work_dir / "exit.code"

        api_key = self._resolve_api_key()
        if not api_key and not self.base_url.startswith("http://127.0.0.1") and not self.base_url.startswith("http://localhost"):
            err = (
                "Direct API anahtarı bulunamadı. "
                "OPENAI_API_KEY veya ANTHROPIC_API_KEY ortam değişkenini tanımlayın."
            )
            stderr_path.write_text(err + "\n", encoding="utf-8")
            exit_code_path.write_text("1\n", encoding="utf-8")
            raw_stream_path.touch(exist_ok=True)
            return WorkerResult(
                ok=False,
                exit_code=1,
                raw_stream_path=raw_stream_path,
                stderr_path=stderr_path,
                backend=self.backend_name,
                error=err,
            )

        owns_client = self._http_client is None
        http = self._http_client or httpx.AsyncClient(timeout=float(req.timeout_secs))
        observed_urls: list[str] = []
        observed_snippets: dict[str, str] = {}

        try:
            prompt_text = req.prompt
            if req.allow_web:
                queries = _extract_queries_from_prompt(req.prompt)
                hits = await search_web_snippets(
                    queries,
                    provider=self.search_provider,
                    client=http,
                )
                if hits:
                    lines = ["\n\n### Canlı Web Arama Bulguları (Yalnız bu URL'leri ve alıntıları kullan):"]
                    for h in hits:
                        url = h["url"]
                        observed_urls.append(url)
                        if h.get("snippet"):
                            observed_snippets[url] = h["snippet"]
                        lines.append(f"- Başlık: {h.get('title', '')}\n  URL: {url}\n  Özet: {h.get('snippet', '')}")
                    prompt_text = req.prompt + "\n".join(lines)

            if req.json_schema is not None:
                prompt_text += (
                    "\n\nYANITINI YALNIZCA GEÇERLİ JSON NESNESİ OLARAK VER "
                    "(ek açıklama veya markdown bloğu ekleme). Şema:\n"
                    + json.dumps(req.json_schema, ensure_ascii=False)
                )

            model_name = req.model or self.default_model
            output_text = await self._call_llm(http, api_key, model_name, prompt_text)
        except Exception as exc:
            err = f"Direct API çağrısı başarısız: {exc}"
            stderr_path.write_text(err + "\n", encoding="utf-8")
            exit_code_path.write_text("1\n", encoding="utf-8")
            raw_stream_path.touch(exist_ok=True)
            return WorkerResult(
                ok=False,
                exit_code=1,
                raw_stream_path=raw_stream_path,
                stderr_path=stderr_path,
                backend=self.backend_name,
                error=err,
            )
        finally:
            if owns_client:
                await http.aclose()

        stream_lines = [
            json.dumps(
                {
                    "type": "search_result",
                    "tool_use_result": [
                        {"url": u, "snippet": observed_snippets.get(u, "")}
                        for u in observed_urls
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "result",
                    "backend": self.backend_name,
                    "provider": self.provider,
                    "output_text": output_text,
                },
                ensure_ascii=False,
            ),
        ]
        raw_stream_path.write_text("\n".join(stream_lines) + "\n", encoding="utf-8")
        exit_code_path.write_text("0\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        parsed = extract_json_from_text(output_text) if req.json_schema is not None else None
        ok = parsed is not None if req.json_schema is not None else bool(output_text.strip())
        return WorkerResult(
            ok=ok,
            exit_code=0 if ok else 1,
            output_text=output_text,
            parsed_json=parsed,
            observed_urls=observed_urls,
            raw_stream_path=raw_stream_path,
            stderr_path=stderr_path,
            backend=self.backend_name,
            error="" if ok else "Direct API yanıtı beklenen JSON şemasına uymadı",
        )

    async def _call_llm(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        model: str,
        prompt: str,
    ) -> str:
        if self.provider == "anthropic":
            url = (self.base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/messages"
            resp = await http.post(
                url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            blocks = payload.get("content", [])
            return "\n".join(
                str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            ).strip()

        # OpenAI-uyumlu uç nokta (OpenAI, OpenRouter, Groq, Ollama vb.)
        base = self.base_url.rstrip("/") or "https://api.openai.com/v1"
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = await http.post(
            url,
            headers=headers,
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        choices = payload.get("choices", [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                return str(msg.get("content", "")).strip()
        return ""


register_adapter(DirectApiAdapter())
