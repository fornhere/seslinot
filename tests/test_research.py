"""Çok-ajanlı derin araştırma motoru, adaptörler, SSRF, Span.verify, denetim ve orkestrasyon testleri."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from seslinot.config import SesliNotConfig
from seslinot.models import (
    ClaimAuditItem,
    QuoteVerification,
    ResearchJob,
    ResearchPhase,
    ResearchPlan,
    SourceRecord,
    SubagentDirective,
    SubagentFinding,
    SubagentOutput,
    VaultContextBundle,
    VaultContextHit,
)
from seslinot.research.adapters import (
    AgyAdapter,
    ClaudeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    WorkerRequest,
    WorkerResult,
    extract_json_from_text,
    extract_snippets_from_stream,
    extract_urls_from_stream,
    get_adapter,
)
from seslinot.research.audit import (
    AuditResponse,
    CitationAuditor,
    extract_claim_triples,
)
from seslinot.research.registry import (
    FetchResponse,
    SourceRegistry,
    canonicalize_url,
    is_ssrf_safe_url,
)
from seslinot.research.runner import ProcessRunner
from seslinot.research.state_machine import ResearchOrchestrator, sanitize_web_queries
from seslinot.research.verifier import (
    audit_draft_citations,
    normalize_for_match,
    render_deterministic_report,
    verify_all_findings,
    verify_quote_against_snapshot,
)


# ---------------------------------------------------------------------------
# 1. CLI Adaptörleri & Çıktı Ayrıştırma Testleri
# ---------------------------------------------------------------------------


def test_claude_adapter_flags_never_bare_and_web_toggle(tmp_path: Path) -> None:
    adapter = ClaudeAdapter()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    req_web = WorkerRequest(
        worker_id="w1",
        role="subagent",
        prompt="PipeWire belgelerini incele",
        system_prompt="Yalnızca gerçek kaynak kullan.",
        work_dir=tmp_path / "claude_web",
        json_schema=schema,
        model="sonnet",
        allow_web=True,
        max_budget_usd=1.25,
    )
    cmd_web, stdin_web, out_path = adapter.build_command(req_web)
    assert out_path is None
    assert stdin_web == b"PipeWire belgelerini incele"
    assert "--bare" not in cmd_web
    assert "--restricted" in cmd_web
    assert "--strict-mcp-config" in cmd_web
    assert "--disable-slash-commands" in cmd_web
    assert "--permission-mode" in cmd_web and cmd_web[cmd_web.index("--permission-mode") + 1] == "dontAsk"
    assert "--permission-prompts" in cmd_web and cmd_web[cmd_web.index("--permission-prompts") + 1] == "none"
    assert "--output-format" in cmd_web and cmd_web[cmd_web.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd_web
    assert "--max-budget-usd" in cmd_web and cmd_web[cmd_web.index("--max-budget-usd") + 1] == "1.25"
    assert cmd_web[cmd_web.index("--tools") + 1] == "Read,Write,Grep,Glob,WebSearch,WebFetch"
    assert cmd_web[cmd_web.index("--allowedTools") + 1] == "Read,Write,Grep,Glob,WebSearch,WebFetch"
    assert cmd_web[cmd_web.index("--disallowedTools") + 1] == "Bash,Edit,NotebookEdit"
    assert "--json-schema" in cmd_web

    req_noweb = req_web.model_copy(
        update={"role": "lead", "allow_web": False, "work_dir": tmp_path / "claude_noweb"}
    )
    cmd_noweb, _, _ = adapter.build_command(req_noweb)
    assert "--bare" not in cmd_noweb
    assert cmd_noweb[cmd_noweb.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd_noweb
    assert cmd_noweb[cmd_noweb.index("--disallowedTools") + 1] == "Bash,Edit,NotebookEdit,Write"


def test_claude_parse_output_and_tool_url_trace_isolation(tmp_path: Path) -> None:
    adapter = ClaudeAdapter()
    stream_file = tmp_path / "raw_stream.jsonl"
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "WebFetch",
                        "input": {"url": "https://docs.pipewire.org/guide?utm_source=test"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "Fetched https://docs.pipewire.org/guide: PipeWire uses 1024 default quantum.",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "Hallucinated link https://never-visited.example/fake should not count.",
                    }
                ]
            },
        },
        {
            "type": "result",
            "is_error": False,
            "result": '{"answer": "ok"}',
            "structured_output": {"answer": "ok"},
        },
    ]
    stream_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    req = WorkerRequest(
        worker_id="w1",
        role="subagent",
        prompt="test",
        work_dir=tmp_path,
        json_schema={"type": "object"},
    )
    res = adapter.parse_output(req, stream_file, None)
    assert res.ok is True
    assert res.parsed_json == {"answer": "ok"}
    assert "https://docs.pipewire.org/guide?utm_source=test" in res.observed_urls
    assert "https://never-visited.example/fake" not in res.observed_urls

    snippets = extract_snippets_from_stream(stream_file)
    assert any("1024 default quantum" in s for v in snippets.values() for s in v)


def test_codex_adapter_flags_strict_schema_and_parse(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    req = WorkerRequest(
        worker_id="w2",
        role="subagent",
        prompt="Araştır",
        system_prompt="Kısa ve doğru ol.",
        work_dir=tmp_path / "codex_w2",
        json_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                },
            },
        },
        model="gpt-6.1-sol",
        reasoning_effort="high",
        allow_web=True,
    )
    cmd, stdin_bytes, out_file = adapter.build_command(req)
    assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in cmd
    assert "--disable" in cmd and cmd[cmd.index("--disable") + 1] == "shell_tool"
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "read-only"
    assert "--color" in cmd and cmd[cmd.index("--color") + 1] == "never"
    assert "--json" in cmd
    assert 'web_search="live"' in cmd
    assert cmd[-1] == "-"
    assert stdin_bytes is not None and b"Ara\xc5\x9ft\xc4\xb1r" in stdin_bytes
    assert out_file is not None

    schema_path = Path(cmd[cmd.index("--output-schema") + 1])
    assert schema_path.is_file()
    strict_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert strict_schema["additionalProperties"] is False
    assert strict_schema["properties"]["nested"]["additionalProperties"] is False

    # allow_web=False durumunda web_search="live" olmamalı
    req_noweb = req.model_copy(update={"allow_web": False, "work_dir": tmp_path / "codex_noweb"})
    cmd_noweb, _, _ = adapter.build_command(req_noweb)
    assert 'web_search="live"' not in cmd_noweb

    # parse_output: -o dosyasını ve stream'deki web_search URL'lerini oku
    out_file.write_text('{"title": "Test", "nested": {"count": 2}}', encoding="utf-8")
    stream_file = req.work_dir / "raw_stream.jsonl"
    stream_file.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "web_search",
                    "results": [{"url": "https://kernel.org/doc/pipewire"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    res = adapter.parse_output(req, stream_file, out_file)
    assert res.ok is True
    assert res.parsed_json == {"title": "Test", "nested": {"count": 2}}
    assert "https://kernel.org/doc/pipewire" in res.observed_urls


def test_agy_and_opencode_adapters_and_registry(tmp_path: Path) -> None:
    agy = AgyAdapter()
    req_agy = WorkerRequest(
        worker_id="a1",
        role="lead",
        prompt="Planla",
        work_dir=tmp_path / "agy",
        json_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        timeout_secs=90,
    )
    cmd_agy, stdin_agy, _ = agy.build_command(req_agy)
    assert cmd_agy[:5] == ["agy", "--sandbox", "--mode", "plan", "--disable-slash-commands"]
    assert "--print-timeout" in cmd_agy and cmd_agy[cmd_agy.index("--print-timeout") + 1] == "90s"
    assert "--output-format" in cmd_agy and cmd_agy[cmd_agy.index("--output-format") + 1] == "stream-json"
    assert "--json-schema" in cmd_agy
    assert cmd_agy[-1].startswith("--print=") and "Planla" in cmd_agy[-1]
    assert stdin_agy is None

    oc = OpenCodeAdapter()
    oc_dir = tmp_path / "opencode"
    req_oc = WorkerRequest(
        worker_id="o1",
        role="subagent",
        prompt="Özetle",
        work_dir=oc_dir,
        model="openai/gpt-5",
        allow_web=True,
    )
    cmd_oc, stdin_oc, _ = oc.build_command(req_oc)
    assert stdin_oc is None
    assert cmd_oc[:4] == ["opencode", "run", "--format", "json"]
    assert "--model" in cmd_oc and cmd_oc[cmd_oc.index("--model") + 1] == "openai/gpt-5"
    assert (oc_dir / "opencode.json").is_file()
    perm_cfg = json.loads((oc_dir / "opencode.json").read_text(encoding="utf-8"))
    assert perm_cfg["permission"]["bash"] == {"*": "deny"}
    assert perm_cfg["permission"]["edit"] == "deny"
    assert perm_cfg["permission"]["write"] == "deny"

    assert get_adapter("claude").backend_name == "claude"
    assert get_adapter("codex").backend_name == "codex"
    assert get_adapter("agy").backend_name == "agy"
    assert get_adapter("opencode").backend_name == "opencode"
    with pytest.raises(ValueError):
        get_adapter("nonexistent_cli")


def test_extract_json_from_text_variants() -> None:
    assert extract_json_from_text('{"a": 1}') == {"a": 1}
    assert extract_json_from_text('İşte yanıt:\n```json\n{"a": "b", "n": 3}\n```') == {
        "a": "b",
        "n": 3,
    }
    assert extract_json_from_text('Ön metin {"k": {"nested": "{brace}"}} son metin') == {
        "k": {"nested": "{brace}"}
    }
    assert extract_json_from_text("geçersiz metin") is None


# ---------------------------------------------------------------------------
# 2. ProcessRunner Testleri
# ---------------------------------------------------------------------------


def test_process_runner_systemd_wrapper_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seslinot.research.runner.shutil.which", lambda name: "/usr/bin/systemd-run")
    runner = ProcessRunner(use_systemd_run=True)
    wrapped, unit = runner.build_wrapped_command(
        ["codex", "exec", "-"],
        unit_name="seslinot-w-job 1",
        timeout_secs=300,
    )
    assert unit == "seslinot-w-job-1"
    assert wrapped[:4] == ["systemd-run", "--user", "--collect", "--wait"]
    assert "--unit=seslinot-w-job-1" in wrapped
    assert "Nice=15" in wrapped
    assert "IOSchedulingClass=idle" in wrapped
    assert "RuntimeMaxSec=300" in wrapped
    assert wrapped[-4:] == ["--", "codex", "exec", "-"]


async def test_process_runner_streams_and_enforces_timeout(tmp_path: Path) -> None:
    class _PythonEchoAdapter:
        backend_name = "py_echo"

        def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
            if req.prompt == "SLEEP":
                return [sys.executable, "-c", "import time; time.sleep(10)"], None, None
            code = (
                "import sys, json; data = sys.stdin.read(); "
                "print(json.dumps({'type': 'result', 'msg': data})); "
                "sys.stderr.write('stderr-line\\n')"
            )
            return [sys.executable, "-c", code], req.prompt.encode("utf-8"), None

        def parse_output(
            self, req: WorkerRequest, stdout_path: Path, output_file_path: Path | None
        ) -> WorkerResult:
            txt = stdout_path.read_text(encoding="utf-8")
            parsed = extract_json_from_text(txt)
            return WorkerResult(ok=parsed is not None, output_text=txt, parsed_json=parsed)

    runner = ProcessRunner(use_systemd_run=False)
    adapter = _PythonEchoAdapter()

    ok_dir = tmp_path / "run_ok"
    res = await runner.run(
        adapter,
        WorkerRequest(worker_id="t1", role="lead", prompt="merhaba", work_dir=ok_dir, timeout_secs=10),
    )
    assert res.ok is True
    assert res.exit_code == 0
    assert res.parsed_json == {"type": "result", "msg": "merhaba"}
    assert (ok_dir / "exit.code").read_text(encoding="utf-8").strip() == "0"
    assert "stderr-line" in (ok_dir / "worker.log").read_text(encoding="utf-8")

    to_dir = tmp_path / "run_timeout"
    res_to = await runner.run(
        adapter,
        WorkerRequest(worker_id="t2", role="lead", prompt="SLEEP", work_dir=to_dir, timeout_secs=1),
    )
    assert res_to.ok is False
    assert res_to.exit_code == 124
    assert (to_dir / "exit.code").read_text(encoding="utf-8").strip() == "124"


# ---------------------------------------------------------------------------
# 3. SourceRegistry, URL Kanonikleştirme & SSRF Koruma Testleri
# ---------------------------------------------------------------------------


def test_canonicalize_url_strips_tracking_and_normalizes() -> None:
    u1 = "HTTPS://Docs.Example.COM:443/guide/audio/?utm_source=x&utm_medium=social&ref=hn&b=2&a=1#section"
    u2 = "https://docs.example.com/guide/audio?a=1&b=2&fbclid=abc&gclid=xyz&mc_cid=99"
    assert canonicalize_url(u1) == "https://docs.example.com/guide/audio?a=1&b=2"
    assert canonicalize_url(u1) == canonicalize_url(u2)
    assert canonicalize_url("http://EXAMPLE.ORG:80/") == "http://example.org"


def test_ssrf_blocks_private_loopback_metadata_and_dns_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_urls = [
        "http://127.0.0.1/admin",
        "http://[::1]:8080/",
        "http://[::ffff:127.0.0.1]/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://172.16.0.5/",
        "http://192.168.1.10/",
        "http://0.0.0.0/",
        "http://localhost:8178/v1/audio",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fe80::1]/",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ]
    for url in blocked_urls:
        safe, reason = is_ssrf_safe_url(url, resolve_dns=False)
        assert safe is False, f"{url} engellenmeliydi (sonuç: {reason})"

    # DNS üzerinden özel IP çözümleme koruması
    def _fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "evil-rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.42", 443))]
        if host == "public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raise socket.gaierror("unknown host")

    monkeypatch.setattr("seslinot.research.registry.socket.getaddrinfo", _fake_getaddrinfo)
    assert is_ssrf_safe_url("https://evil-rebind.example/a", resolve_dns=True)[0] is False
    assert is_ssrf_safe_url("https://public.example/a", resolve_dns=True)[0] is True
    assert is_ssrf_safe_url("https://missing.example/a", resolve_dns=True)[0] is False


async def test_source_registry_dedup_fetch_ssrf_redirect_and_vault(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    (vault_dir / "notlar").mkdir(parents=True)
    (vault_dir / "notlar/audio.md").write_text(
        "# PipeWire Notları\nVarsayılan quantum 1024 örnektir.\n",
        encoding="utf-8",
    )

    cfg = SesliNotConfig()
    cfg.vault.path = vault_dir
    sess_dir = tmp_path / "session"

    async def _mock_fetcher(url: str) -> FetchResponse:
        if url == "https://docs.pipewire.org/latency":
            return FetchResponse(
                status_code=200,
                text="<html><head><title>PipeWire Latency</title></head><body>"
                "<article><p>PipeWire supports low-latency audio scheduling with quantum 128 at 48kHz.</p></article>"
                "</body></html>",
                final_url=url,
            )
        if url == "https://paywalled.example/article":
            return FetchResponse(status_code=403, text="Forbidden", final_url=url)
        if url == "https://redirect-ssrf.example/go":
            return FetchResponse(
                status_code=200,
                text="secret metadata",
                final_url="http://169.254.169.254/latest/meta-data",
            )
        return FetchResponse(status_code=404, text="Not Found", final_url=url)

    reg = SourceRegistry(sess_dir, vault_dir, cfg, fetcher=_mock_fetcher)
    bundle = VaultContextBundle(
        query="PipeWire",
        hits=[
            VaultContextHit(
                source_id="V01",
                path="notlar/audio.md",
                title="PipeWire Notları",
                excerpt="Varsayılan quantum 1024 örnektir.",
            ),
            VaultContextHit(
                source_id="V02",
                path="../../etc/passwd",
                title="Traversal",
                excerpt="engellenmeli",
            ),
        ],
    )
    reg.ingest_vault_context(bundle)

    outputs = [
        SubagentOutput(
            worker_id="w1",
            subtopic="latency",
            findings=[
                SubagentFinding(
                    claim_id="c1",
                    claim_tr="PipeWire 48kHz'de 128 quantum destekler.",
                    exact_quote="PipeWire supports low-latency audio scheduling with quantum 128 at 48kHz.",
                    url="https://docs.pipewire.org/latency/?utm_source=hn",
                    title="PipeWire Latency",
                ),
                SubagentFinding(
                    claim_id="c2",
                    claim_tr="Ödeme duvarlı makale.",
                    exact_quote="Snippet quote seen in tool trace",
                    url="https://paywalled.example/article",
                    title="Paywalled",
                ),
            ],
        ),
        SubagentOutput(
            worker_id="w2",
            subtopic="dedup",
            findings=[
                SubagentFinding(
                    claim_id="c3",
                    claim_tr="Aynı URL farklı izleme parametresiyle.",
                    exact_quote="quantum 128 at 48kHz",
                    url="https://docs.pipewire.org/latency?ref=blog",
                ),
                SubagentFinding(
                    claim_id="c4",
                    claim_tr="SSRF yönlendirmesi engellenmeli.",
                    exact_quote="secret metadata",
                    url="https://redirect-ssrf.example/go",
                ),
                SubagentFinding(
                    claim_id="c5",
                    claim_tr="Kırık bağlantı.",
                    exact_quote="missing",
                    url="https://broken.example/404",
                ),
            ],
        ),
    ]

    observed = {
        "w1": [
            "https://docs.pipewire.org/latency/?utm_source=hn",
            "https://paywalled.example/article",
        ],
        "w2": ["https://docs.pipewire.org/latency?ref=blog"],
    }
    fmap = reg.ingest_subagent_outputs(outputs, observed)
    records = await reg.fetch_all()

    assert reg.records["V01"].fetch_status == "vault_ok"
    assert reg.records["V02"].fetch_status == "vault_missing"

    # c1 ve c3 aynı kanonik S01 kaydına tekilleştirilmeli
    assert fmap[("w1", "c1")] == fmap[("w2", "c3")] == "S01"
    s01 = reg.records["S01"]
    assert s01.fetch_status == "fetched_ok"
    assert s01.snapshot_path is not None and Path(s01.snapshot_path).is_file()
    assert s01.snapshot_sha256 is not None
    assert set(s01.referenced_by_workers) == {"w1", "w2"}

    # 403 + seen_in_tool_trace=True -> fetch_blocked_snippet_only
    s02 = reg.records[fmap[("w1", "c2")]]
    assert s02.fetch_status == "fetch_blocked_snippet_only"

    # SSRF yönlendirmesi ve 404 -> dead_url
    assert reg.records[fmap[("w2", "c4")]].fetch_status == "dead_url"
    assert reg.records[fmap[("w2", "c5")]].fetch_status == "dead_url"

    # sources.jsonl kalıcı olarak yazılmış ve yüklenebilir olmalı
    reloaded = SourceRegistry.load_records(sess_dir)
    assert len(reloaded) == len(records)
    assert reloaded["S01"].snapshot_sha256 == s01.snapshot_sha256


# ---------------------------------------------------------------------------
# 4. Span.verify, Hayalet Atıf & Deterministik Kaynakça Testleri
# ---------------------------------------------------------------------------


def test_span_verify_exact_near_ungrounded_and_dead(tmp_path: Path) -> None:
    snap_path = tmp_path / "S01.txt"
    snap_text = (
        "İstanbul'da yapılan testlerde **PipeWire** — düşük gecikmeli ses sunucusu — "
        "“quantum=128” ayarında 2.67 milisaniye tampon süresiyle kararlı çalışmaktadır."
    )
    snap_path.write_text(snap_text, encoding="utf-8")

    # Normalize & Exact match (akıllı tırnak, markdown, Türkçe İ)
    assert "istanbul'da" in normalize_for_match(snap_text)
    st_exact, sc_exact = verify_quote_against_snapshot(
        'istanbul\'da yapılan testlerde PipeWire - düşük gecikmeli ses sunucusu - "quantum=128"',
        snap_text,
    )
    assert st_exact == "exact_match"
    assert sc_exact == 100.0

    # Near match (RapidFuzz >= 88, uzunluk >= 25)
    st_near, sc_near = verify_quote_against_snapshot(
        "quantum=128 ayarinda 2.67 milisaniye tampon suresiyle kararl1 calismaktadir",
        snap_text,
    )
    assert st_near == "near_match"
    assert sc_near >= 88.0

    # Ungrounded quote
    st_bad, sc_bad = verify_quote_against_snapshot(
        "Tamamen uydurma ve kaynakta hic gecmeyen bir alinti metni burada yer aliyor.",
        snap_text,
    )
    assert st_bad == "ungrounded_quote"
    assert sc_bad < 88.0

    registry = {
        "S01": SourceRecord(
            source_id="S01",
            source_type="web",
            canonical_url="https://docs.example/pw",
            final_url="https://docs.example/pw",
            title="PW",
            fetch_status="fetched_ok",
            snapshot_path=str(snap_path),
        ),
        "S02": SourceRecord(
            source_id="S02",
            source_type="web",
            canonical_url="https://blocked.example/p",
            title="Blocked",
            fetch_status="fetch_blocked_snippet_only",
            seen_in_tool_trace=True,
        ),
        "S03": SourceRecord(
            source_id="S03",
            source_type="web",
            canonical_url="https://dead.example/404",
            title="Dead",
            fetch_status="dead_url",
        ),
    }

    outputs = [
        SubagentOutput(
            worker_id="w1",
            subtopic="test",
            findings=[
                SubagentFinding(
                    claim_id="f1",
                    claim_tr="Doğru alıntı",
                    exact_quote="quantum=128” ayarında 2.67 milisaniye tampon süresiyle kararlı çalışmaktadır.",
                    url="https://docs.example/pw",
                ),
                SubagentFinding(
                    claim_id="f2",
                    claim_tr="Engelli kaynak özeti",
                    exact_quote="Blocked article snippet confirms benchmark improvement of 40 percent.",
                    url="https://blocked.example/p",
                ),
                SubagentFinding(
                    claim_id="f3",
                    claim_tr="Ölü kaynak",
                    exact_quote="Whatever",
                    url="https://dead.example/404",
                ),
            ],
        )
    ]
    checks = verify_all_findings(
        outputs,
        registry,
        tool_snippets={
            "https://blocked.example/p": [
                "Search snippet: Blocked article snippet confirms benchmark improvement of 40 percent."
            ]
        },
    )
    by_id = {c.claim_id: c for c in checks}
    assert by_id["f1"].quote_status == "exact_match"
    assert by_id["f2"].quote_status == "snippet_only"
    assert by_id["f3"].quote_status == "dead_source"


def test_audit_draft_citations_and_deterministic_bibliography() -> None:
    registry = {
        "V01": SourceRecord(
            source_id="V01",
            source_type="vault",
            vault_path="notlar/pipewire-ayarlari.md",
            title="PipeWire Ayarlarım",
            fetch_status="vault_ok",
            retrieved_at="2026-11-22T18:00:00+00:00",
        ),
        "S01": SourceRecord(
            source_id="S01",
            source_type="web",
            canonical_url="https://docs.pipewire.org/latency",
            final_url="https://docs.pipewire.org/latency",
            title="PipeWire Resmi Gecikme Rehberi",
            published_at="2026-10-15",
            fetch_status="fetched_ok",
            retrieved_at="2026-11-22T18:00:00+00:00",
        ),
        "S02": SourceRecord(
            source_id="S02",
            source_type="web",
            canonical_url="https://dead.example/404",
            title="Ölü Sayfa",
            fetch_status="dead_url",
        ),
    }

    draft = (
        "# Yönetici Özeti\n\n"
        "Mevcut notlarda 1024 quantum kullanılıyor.[^V01] "
        "Resmi belgeler 128 quantum öneriyor.[^S01] "
        "Uydurma iddia.[^S99] Ölü kaynak iddiası.[^S02] "
        "Sızan ham bağlantı: https://leaked.example/secret.\n\n"
        "## Kaynaklar\n\n"
        "- [Sahte LLM Kaynağı](https://fake.example/llm)\n"
        "[^S01]: Sahte dipnot\n"
    )

    ghosts, leaked = audit_draft_citations(draft, registry)
    assert "[^S99]" in ghosts
    assert "[^S02]" in ghosts
    assert "https://leaked.example/secret" in leaked

    final_md, cited = render_deterministic_report(draft, registry)
    assert [c.source_id for c in cited] == ["V01", "S01"]
    assert "[^1]" in final_md and "[^2]" in final_md
    assert "[^S99]" not in final_md
    assert "[^S02]" not in final_md
    assert "Sahte LLM Kaynağı" not in final_md
    assert "[[notlar/pipewire-ayarlari|PipeWire Ayarlarım]]" in final_md
    assert "[PipeWire Resmi Gecikme Rehberi](https://docs.pipewire.org/latency)" in final_md
    assert "[^1]: [[notlar/pipewire-ayarlari|PipeWire Ayarlarım]]" in final_md
    assert "[^2]: [PipeWire Resmi Gecikme Rehberi](https://docs.pipewire.org/latency)" in final_md


# ---------------------------------------------------------------------------
# 5. Çapraz Şirket Denetçi & Fail-Closed Testleri
# ---------------------------------------------------------------------------


def test_auditor_selects_cross_company_and_extracts_triples() -> None:
    cfg = SesliNotConfig()
    auditor = CitationAuditor(cfg)

    assert auditor.select_audit_backends("claude")[0][0] == "codex"
    assert auditor.select_audit_backends("codex")[0][0] == "claude"

    draft = (
        "PipeWire varsayılan olarak 1024 quantum kullanır.[^S01] "
        "Düşük gecikme için 128 önerilir.[^S02]\n"
    )
    qchecks = [
        QuoteVerification(
            claim_id="c1",
            worker_id="w1",
            source_id="S01",
            quote_status="exact_match",
            similarity=100.0,
            claim_tr="1024 quantum",
            exact_quote="Default quantum is 1024 samples.",
        ),
        QuoteVerification(
            claim_id="c2",
            worker_id="w2",
            source_id="S02",
            quote_status="near_match",
            similarity=92.0,
            claim_tr="128 quantum",
            exact_quote="Use quantum 128 for low latency.",
        ),
    ]
    triples = extract_claim_triples(draft, qchecks, {})
    assert len(triples) == 2
    assert triples[0]["source_ids"] == ["S01"]
    assert triples[1]["source_ids"] == ["S02"]


async def test_auditor_fail_closed_marks_unrepaired_contradiction(tmp_path: Path) -> None:
    cfg = SesliNotConfig()
    cfg.research.repair_max_rounds = 0  # Onarım kapalı -> doğrudan fail-closed

    class _MockAdapter:
        def __init__(self, name: str) -> None:
            self.backend_name = name

        def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
            return ["mock"], None, None

        def parse_output(
            self, req: WorkerRequest, stdout_path: Path, output_file_path: Path | None
        ) -> WorkerResult:
            return WorkerResult(ok=True)

    class _MockAuditRunner(ProcessRunner):
        async def run(self, adapter: Any, req: WorkerRequest) -> WorkerResult:
            payload = AuditResponse(
                items=[
                    ClaimAuditItem(
                        sentence="PipeWire yalnızca Windows'ta çalışır.[^S01]",
                        source_ids=["S01"],
                        verdict="CONTRADICTED",
                        note="Kaynak Linux'tan bahsediyor.",
                    )
                ]
            ).model_dump()
            return WorkerResult(ok=True, parsed_json=payload, output_text=json.dumps(payload))

    registry = {
        "S01": SourceRecord(
            source_id="S01",
            source_type="web",
            canonical_url="https://docs.pipewire.org",
            final_url="https://docs.pipewire.org",
            title="PipeWire",
            fetch_status="fetched_ok",
        )
    }
    qchecks = [
        QuoteVerification(
            claim_id="c1",
            worker_id="w1",
            source_id="S01",
            quote_status="exact_match",
            similarity=100.0,
            claim_tr="Linux",
            exact_quote="PipeWire is a Linux audio server.",
        )
    ]
    draft = (
        "PipeWire yalnızca Windows'ta çalışır.[^S01] "
        "Hayalet atıf.[^S99] Ham link https://leak.example/x"
    )
    auditor = CitationAuditor(
        cfg,
        adapter_factory=lambda name: _MockAdapter(name),
        runner=_MockAuditRunner(use_systemd_run=False),
    )
    gated, vrep = await auditor.audit_and_repair(
        draft, qchecks, registry, tmp_path / "audit", synth_backend="claude"
    )
    assert vrep.audit_status == "failed_closed"
    assert "[Doğrulanamadı]" in gated
    assert "[^S99]" not in gated
    assert "https://leak.example/x" not in gated


# ---------------------------------------------------------------------------
# 6. Tam Uçtan Uca 4+1 Faz Orkestrasyon, Fallback, Onarım ve Crash-Resume Testleri
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, name: str) -> None:
        self.backend_name = name

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        return [self.backend_name], None, None

    def parse_output(
        self, req: WorkerRequest, stdout_path: Path, output_file_path: Path | None
    ) -> WorkerResult:
        return WorkerResult(ok=True, backend=self.backend_name)


async def test_orchestrator_end_to_end_fallback_audit_repair_and_privacy(tmp_path: Path) -> None:
    vault_dir = tmp_path / "HafizaTest"
    (vault_dir / "notlar").mkdir(parents=True)
    (vault_dir / "notlar/pipewire.md").write_text(
        "# PipeWire Mevcut Ayarlar\nSistemde şu anda quantum 1024 kullanılıyor.\n",
        encoding="utf-8",
    )

    cfg = SesliNotConfig()
    cfg.vault.path = vault_dir
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.paths.ensure_dirs()
    cfg.research.use_systemd_run = False
    cfg.research.repair_max_rounds = 1

    calls: list[tuple[str, str, str]] = []  # (role, backend, worker_id)
    audit_round = {"count": 0}

    class _ScenarioRunner(ProcessRunner):
        async def run(self, adapter: Any, req: WorkerRequest) -> WorkerResult:
            req.work_dir.mkdir(parents=True, exist_ok=True)
            calls.append((req.role, adapter.backend_name, req.worker_id))

            if req.role == "lead":
                plan = ResearchPlan(
                    title_tr="PipeWire Düşük Gecikme Optimizasyonu",
                    topic_slug="pipewire-dusuk-gecikme",
                    scope="user",
                    brief_summary="PipeWire quantum ve RT öncelik optimizasyonu.",
                    key_questions=[
                        "Düşük gecikme için ideal quantum değeri nedir?",
                        "RTKit önceliği nasıl ayarlanır?",
                    ],
                    concepts=["pipewire", "linux-ses"],
                    directives=[
                        SubagentDirective(
                            worker_id="worker_01",
                            subtopic="Quantum ve Tampon Ayarları",
                            objective="Resmi PipeWire gecikme belgelerini tara.",
                            queries_en=[
                                "PipeWire low latency quantum /home/forn/HafizaTest/notlar/pipewire.md",
                            ],
                            queries_tr=["PipeWire düşük gecikme HafizaTest"],
                        ),
                        SubagentDirective(
                            worker_id="worker_02",
                            subtopic="RTKit ve Çekirdek Planlayıcı",
                            objective="RT öncelik gereksinimlerini araştır.",
                            queries_en=["PipeWire RTKit real-time scheduling"],
                            queries_tr=["PipeWire RTKit gerçek zamanlı"],
                        ),
                    ],
                )
                return WorkerResult(
                    ok=True,
                    parsed_json=plan.model_dump(),
                    output_text=plan.model_dump_json(),
                    backend=adapter.backend_name,
                )

            if req.role == "subagent" and req.worker_id == "worker_01":
                # Birincil codex worker başarısız olsun -> claude fallback devreye girsin!
                if adapter.backend_name == "codex":
                    return WorkerResult(
                        ok=False,
                        exit_code=1,
                        backend="codex",
                        error="simulated codex failure",
                    )
                stream_path = req.work_dir / "raw_stream.jsonl"
                stream_path.write_text(
                    json.dumps(
                        {
                            "type": "tool_result",
                            "url": "https://docs.pipewire.org/latency",
                            "content": "Setting clock.force-quantum to 128 reduces latency to 2.67ms at 48kHz.",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                out = SubagentOutput(
                    worker_id="worker_01",
                    subtopic="Quantum ve Tampon Ayarları",
                    status="completed",
                    findings=[
                        SubagentFinding(
                            claim_id="w1_c1",
                            claim_tr="48kHz'de 128 quantum gecikmeyi 2.67ms düzeyine indirir.",
                            exact_quote="Setting clock.force-quantum to 128 reduces latency to 2.67ms at 48kHz.",
                            url="https://docs.pipewire.org/latency?utm_source=search",
                            title="PipeWire Latency Docs",
                            published_at="2026-09-10",
                        )
                    ],
                )
                return WorkerResult(
                    ok=True,
                    parsed_json=out.model_dump(),
                    output_text=out.model_dump_json(),
                    raw_stream_path=stream_path,
                    observed_urls=extract_urls_from_stream(stream_path),
                    backend=adapter.backend_name,
                )

            if req.role == "subagent" and req.worker_id == "worker_02":
                stream_path = req.work_dir / "raw_stream.jsonl"
                stream_path.write_text(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "web_search",
                                "results": [{"url": "https://wiki.archlinux.org/title/PipeWire"}],
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                out = SubagentOutput(
                    worker_id="worker_02",
                    subtopic="RTKit ve Çekirdek Planlayıcı",
                    status="completed",
                    findings=[
                        SubagentFinding(
                            claim_id="w2_c1",
                            claim_tr="RTKit servisi PipeWire iş parçacıklarına gerçek zamanlı öncelik verir.",
                            exact_quote="RTKit grants real-time scheduling priority to PipeWire audio threads.",
                            url="https://wiki.archlinux.org/title/PipeWire",
                            title="ArchWiki PipeWire",
                            published_at="2026-10-01",
                        ),
                        SubagentFinding(
                            claim_id="w2_c_bad_quote",
                            claim_tr="Uydurma alıntı elenmeli.",
                            exact_quote="This fabricated sentence does not exist anywhere on ArchWiki.",
                            url="https://wiki.archlinux.org/title/PipeWire",
                        ),
                        SubagentFinding(
                            claim_id="w2_c_dead",
                            claim_tr="Ölü bağlantı elenmeli.",
                            exact_quote="Dead quote",
                            url="https://dead.example/404",
                        ),
                    ],
                )
                return WorkerResult(
                    ok=True,
                    parsed_json=out.model_dump(),
                    output_text=out.model_dump_json(),
                    raw_stream_path=stream_path,
                    observed_urls=extract_urls_from_stream(stream_path),
                    backend=adapter.backend_name,
                )

            if req.role == "synthesizer":
                if "repair" in req.worker_id:
                    # Onarılmış temiz taslak
                    repaired_md = (
                        "## Yönetici Özeti\n\n"
                        "Mevcut kasadaki notlarda 1024 quantum kullanılmaktadır.[^V01] "
                        "48kHz örnekleme hızında `clock.force-quantum=128` ayarı gecikmeyi 2.67ms'ye indirir.[^S01] "
                        "RTKit servisi ses iş parçacıklarına gerçek zamanlı öncelik sağlar.[^S02]\n"
                    )
                    return WorkerResult(ok=True, output_text=repaired_md, backend=adapter.backend_name)

                # İlk taslakta kasıtlı CONTRADICTED iddia + hayalet atıf [^S99]
                initial_md = (
                    "## Yönetici Özeti\n\n"
                    "Mevcut kasadaki notlarda 1024 quantum kullanılmaktadır.[^V01] "
                    "48kHz'de 128 quantum ayarı gecikmeyi 500ms'ye çıkarır.[^S01] "
                    "RTKit servisi gerçek zamanlı öncelik sağlar.[^S02] "
                    "Bilinmeyen iddia.[^S99]\n"
                )
                return WorkerResult(ok=True, output_text=initial_md, backend=adapter.backend_name)

            if req.role == "auditor":
                assert adapter.backend_name == "codex"  # Claude sentez -> Codex çapraz şirket denetçi!
                assert req.allow_web is False
                audit_round["count"] += 1
                if audit_round["count"] == 1:
                    resp = AuditResponse(
                        items=[
                            ClaimAuditItem(
                                sentence="48kHz'de 128 quantum ayarı gecikmeyi 500ms'ye çıkarır.[^S01]",
                                source_ids=["S01"],
                                verdict="CONTRADICTED",
                                note="Kaynak 2.67ms diyor, 500ms değil.",
                            )
                        ]
                    )
                else:
                    resp = AuditResponse(
                        items=[
                            ClaimAuditItem(
                                sentence="48kHz örnekleme hızında `clock.force-quantum=128` ayarı gecikmeyi 2.67ms'ye indirir.[^S01]",
                                source_ids=["S01"],
                                verdict="SUPPORTED",
                            ),
                            ClaimAuditItem(
                                sentence="RTKit servisi ses iş parçacıklarına gerçek zamanlı öncelik sağlar.[^S02]",
                                source_ids=["S02"],
                                verdict="SUPPORTED",
                            ),
                        ]
                    )
                return WorkerResult(
                    ok=True,
                    parsed_json=resp.model_dump(),
                    output_text=resp.model_dump_json(),
                    backend=adapter.backend_name,
                )

            raise AssertionError(f"Beklenmeyen çağrı: {req.role}/{req.worker_id}")

    async def _mock_fetcher(url: str) -> FetchResponse:
        if url == "https://docs.pipewire.org/latency":
            return FetchResponse(
                status_code=200,
                text="<html><body><article><p>Setting clock.force-quantum to 128 reduces latency to 2.67ms at 48kHz.</p></article></body></html>",
                final_url=url,
            )
        if url == "https://wiki.archlinux.org/title/PipeWire":
            return FetchResponse(
                status_code=200,
                text="<html><body><article><p>RTKit grants real-time scheduling priority to PipeWire audio threads.</p></article></body></html>",
                final_url=url,
            )
        return FetchResponse(status_code=404, text="Not found", final_url=url)

    seen_phases: list[ResearchPhase] = []

    orchestrator = ResearchOrchestrator(
        cfg,
        adapter_factory=lambda name: _FakeAdapter(name),
        runner=_ScenarioRunner(use_systemd_run=False),
        fetcher=_mock_fetcher,
        progress_cb=lambda phase, _detail: seen_phases.append(phase),
    )

    job = ResearchJob(
        job_id="job-e2e-001",
        note_id="note-1",
        topic="PipeWire düşük gecikme",
        raw_prompt="PipeWire düşük gecikme ve RTKit ayarlarını araştır.",
    )
    bundle = VaultContextBundle(
        query="PipeWire",
        matched_concepts=["pipewire"],
        hits=[
            VaultContextHit(
                source_id="V01",
                path="notlar/pipewire.md",
                title="PipeWire Mevcut Ayarlar",
                excerpt="Sistemde şu anda quantum 1024 kullanılıyor.",
            )
        ],
    )

    sess_dir = tmp_path / "data/sessions/test-e2e-001"
    report = await orchestrator.run(
        job,
        vault_context=bundle,
        session_dir=sess_dir,
        triggered_by_rel_path="sesli-notlar/2026-11/note-1.md",
    )

    # Faz sıralaması doğru ilerlemeli
    assert seen_phases == [
        ResearchPhase.VAULT_SCAN,
        ResearchPhase.DIRECTION,
        ResearchPhase.GATHERING,
        ResearchPhase.VERIFYING,
        ResearchPhase.SYNTHESIZING,
        ResearchPhase.AUDITING,
    ]

    # Gizlilik sanitizasyonu: yerel yol ve kasa adı frozen worker promptundan temizlenmiş olmalı
    w1_prompt = (sess_dir / "directives/worker_01.prompt.md").read_text(encoding="utf-8")
    assert "/home/forn" not in w1_prompt
    assert "HafizaTest" not in w1_prompt

    # Worker 1 codex -> claude fallback çalışmış olmalı
    assert ("subagent", "codex", "worker_01") in calls
    assert ("subagent", "claude", "worker_01") in calls
    # Çapraz şirket denetimi (claude synth -> codex audit) ve 1 tur onarım çalışmış olmalı
    assert ("auditor", "codex", "auditor_codex") in calls
    assert ("synthesizer", "claude", "synthesizer_repair_1") in calls
    assert audit_round["count"] == 2

    # Nihai rapor ve doğrulama sonuçları
    assert report.report_id == "job-e2e-001"
    assert report.title == "PipeWire Düşük Gecikme Optimizasyonu"
    assert report.confidence == "yüksek"
    assert report.verification.audit_status == "repaired"
    assert report.verification.ghost_citations == []
    assert report.verification.leaked_urls == []
    assert report.verification.quote_exact_match == 2
    assert report.verification.quote_ungrounded == 1
    assert report.verification.dead_sources == 1
    assert [s.source_id for s in report.sources] == ["V01", "S01", "S02"]
    assert "[[notlar/pipewire|PipeWire Mevcut Ayarlar]]" in report.body_markdown
    assert "[PipeWire Latency Docs](https://docs.pipewire.org/latency)" in report.body_markdown
    assert "[ArchWiki PipeWire](https://wiki.archlinux.org/title/PipeWire)" in report.body_markdown
    assert "500ms" not in report.body_markdown
    assert "[^S99]" not in report.body_markdown
    assert (sess_dir / "verification.json").is_file()
    assert (sess_dir / "final.md").is_file()
    assert (sess_dir / "sources/sources.jsonl").is_file()


async def test_orchestrator_crash_resume_skips_completed_worker(tmp_path: Path) -> None:
    cfg = SesliNotConfig()
    cfg.vault.path = tmp_path / "vault"
    cfg.vault.path.mkdir()
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.paths.ensure_dirs()
    cfg.research.use_systemd_run = False

    sess_dir = tmp_path / "data/sessions/resume-001"
    (sess_dir / "directives").mkdir(parents=True)
    (sess_dir / "workers/worker_01").mkdir(parents=True)

    # Önceki yarım kalmış oturumdan kalan plan ve tamamlanmış worker_01 çıktısı
    plan = ResearchPlan(
        title_tr="Devam Eden Araştırma",
        topic_slug="devam-eden-arastirma",
        brief_summary="Crash-resume testi.",
        key_questions=["Soru 1", "Soru 2"],
        directives=[
            SubagentDirective(
                worker_id="worker_01",
                subtopic="Bölüm 1",
                objective="Hedef 1",
                queries_en=["query one"],
            ),
            SubagentDirective(
                worker_id="worker_02",
                subtopic="Bölüm 2",
                objective="Hedef 2",
                queries_en=["query two"],
            ),
        ],
    )
    (sess_dir / "directives/plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    (sess_dir / "directives/worker_01.prompt.md").write_text("Prompt 1", encoding="utf-8")
    (sess_dir / "directives/worker_02.prompt.md").write_text("Prompt 2", encoding="utf-8")

    w1_cached = SubagentOutput(
        worker_id="worker_01",
        subtopic="Bölüm 1",
        status="completed",
        findings=[
            SubagentFinding(
                claim_id="w1_cached_1",
                claim_tr="Önbellekten gelen doğrulanmış bulgu.",
                exact_quote="Cached worker one verified finding text from official docs.",
                url="https://docs.example/one",
                title="Docs One",
            )
        ],
    )
    (sess_dir / "workers/worker_01/findings.json").write_text(
        w1_cached.model_dump_json(indent=2), encoding="utf-8"
    )
    (sess_dir / "workers/worker_01/observed_urls.json").write_text(
        json.dumps(["https://docs.example/one"]), encoding="utf-8"
    )

    executed: list[tuple[str, str]] = []

    class _ResumeRunner(ProcessRunner):
        async def run(self, adapter: Any, req: WorkerRequest) -> WorkerResult:
            executed.append((req.role, req.worker_id))
            if req.role == "subagent" and req.worker_id == "worker_02":
                out = SubagentOutput(
                    worker_id="worker_02",
                    subtopic="Bölüm 2",
                    status="completed",
                    findings=[
                        SubagentFinding(
                            claim_id="w2_fresh_1",
                            claim_tr="Yeni çalışan worker_02 bulgusu.",
                            exact_quote="Fresh worker two verified finding text from official docs.",
                            url="https://docs.example/two",
                            title="Docs Two",
                        )
                    ],
                )
                return WorkerResult(
                    ok=True,
                    parsed_json=out.model_dump(),
                    output_text=out.model_dump_json(),
                    observed_urls=["https://docs.example/two"],
                    backend=adapter.backend_name,
                )
            if req.role == "synthesizer":
                return WorkerResult(
                    ok=True,
                    output_text=(
                        "Birinci bulgu önbellekten geldi.[^S01] "
                        "İkinci bulgu yeni çalıştırıldı.[^S02]\n"
                    ),
                    backend=adapter.backend_name,
                )
            if req.role == "auditor":
                resp = AuditResponse(
                    items=[
                        ClaimAuditItem(
                            sentence="Birinci bulgu önbellekten geldi.[^S01]",
                            source_ids=["S01"],
                            verdict="SUPPORTED",
                        ),
                        ClaimAuditItem(
                            sentence="İkinci bulgu yeni çalıştırıldı.[^S02]",
                            source_ids=["S02"],
                            verdict="SUPPORTED",
                        ),
                    ]
                )
                return WorkerResult(
                    ok=True,
                    parsed_json=resp.model_dump(),
                    output_text=resp.model_dump_json(),
                    backend=adapter.backend_name,
                )
            raise AssertionError(f"Beklenmeyen yeniden çalıştırma: {req.role}/{req.worker_id}")

    async def _mock_fetcher(url: str) -> FetchResponse:
        if url == "https://docs.example/one":
            return FetchResponse(
                status_code=200,
                text="<p>Cached worker one verified finding text from official docs.</p>",
                final_url=url,
            )
        if url == "https://docs.example/two":
            return FetchResponse(
                status_code=200,
                text="<p>Fresh worker two verified finding text from official docs.</p>",
                final_url=url,
            )
        return FetchResponse(status_code=404, text="", final_url=url)

    orchestrator = ResearchOrchestrator(
        cfg,
        adapter_factory=lambda name: _FakeAdapter(name),
        runner=_ResumeRunner(use_systemd_run=False),
        fetcher=_mock_fetcher,
    )
    job = ResearchJob(
        job_id="resume-001",
        note_id="note-r",
        topic="Devam Eden Araştırma",
        raw_prompt="Devam eden araştırma transkripti.",
    )
    report = await orchestrator.run(job, session_dir=sess_dir)

    # Lead ve tamamlanmış worker_01 KESİNLİKLE yeniden çalıştırılmamış olmalı!
    assert ("lead", "lead") not in executed
    assert ("subagent", "worker_01") not in executed
    assert ("subagent", "worker_02") in executed
    assert report.verification.audit_status == "passed"
    assert [s.source_id for s in report.sources] == ["S01", "S02"]
    assert "https://docs.example/one" in report.body_markdown
    assert "https://docs.example/two" in report.body_markdown


def test_sanitize_web_queries_strips_emails_tokens_and_paths(tmp_path: Path) -> None:
    plan = ResearchPlan(
        title_tr="Test",
        topic_slug="test",
        brief_summary="Test",
        directives=[
            SubagentDirective(
                worker_id="w1",
                subtopic="s",
                objective="o",
                queries_en=[
                    "PipeWire user@example.com sk-1234567890abcdef /home/forn/Hafıza/gizli.md config"
                ],
                queries_tr=["~/Hafıza/notlar/ozel-proje.md PipeWire gecikme"],
            )
        ],
    )
    cleaned = sanitize_web_queries(plan, None, tmp_path / "Hafıza")
    q_en = cleaned.directives[0].queries_en[0]
    q_tr = cleaned.directives[0].queries_tr[0]
    assert "user@example.com" not in q_en
    assert "sk-1234567890abcdef" not in q_en
    assert "/home/forn" not in q_en
    assert "PipeWire" in q_en and "config" in q_en
    assert "~/Hafıza" not in q_tr
    assert "ozel-proje.md" not in q_tr
    assert "PipeWire gecikme" in q_tr


async def test_source_registry_httpx_mock_transport_blocks_redirect_ssrf(tmp_path: Path) -> None:
    """Gerçek httpx.AsyncClient(follow_redirects=True) yolunda 302 -> metadata IP yönlendirmesi engellenir."""
    requested_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if url == "https://evil-redir.example/start":
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data"},
            )
        if url == "https://safe-redir.example/start":
            return httpx.Response(
                302,
                headers={"Location": "https://safe-redir.example/final"},
            )
        if url == "https://safe-redir.example/final":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><head><title>Safe Final</title></head><body>"
                "<article><p>Safe redirected document verified via httpx AsyncClient.</p></article>"
                "</body></html>",
            )
        return httpx.Response(404, text="missing")

    cfg = SesliNotConfig()
    cfg.vault.path = tmp_path / "vault"
    cfg.vault.path.mkdir()

    reg = SourceRegistry(
        tmp_path / "sess_httpx",
        cfg.vault.path,
        cfg,
        transport=httpx.MockTransport(_handler),
    )
    outputs = [
        SubagentOutput(
            worker_id="w1",
            subtopic="redir",
            findings=[
                SubagentFinding(
                    claim_id="r1",
                    claim_tr="SSRF 302 engellenmeli",
                    exact_quote="metadata",
                    url="https://evil-redir.example/start",
                ),
                SubagentFinding(
                    claim_id="r2",
                    claim_tr="Güvenli 302 takip edilmeli",
                    exact_quote="Safe redirected document verified via httpx AsyncClient.",
                    url="https://safe-redir.example/start",
                ),
            ],
        )
    ]
    fmap = reg.ingest_subagent_outputs(outputs, {})
    await reg.fetch_all()

    assert reg.records[fmap[("w1", "r1")]].fetch_status == "dead_url"
    # SSRF request hook metadata IP'ye İSTEK GÖNDERMEDEN önce durdurmalı!
    assert "http://169.254.169.254/latest/meta-data" not in requested_urls

    safe_rec = reg.records[fmap[("w1", "r2")]]
    assert safe_rec.fetch_status == "fetched_ok"
    assert safe_rec.final_url == "https://safe-redir.example/final"
    assert safe_rec.title == "Safe Final"


def test_agy_and_opencode_parse_output(tmp_path: Path) -> None:
    agy = AgyAdapter()
    agy_stream = tmp_path / "agy.jsonl"
    agy_stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_result",
                        "url": "https://example.org/agy-doc",
                        "content": "verified snippet",
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "result": '{"status": "ok"}',
                        "structured_output": {"status": "ok"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    req_agy = WorkerRequest(
        worker_id="a1",
        role="subagent",
        prompt="p",
        work_dir=tmp_path,
        json_schema={"type": "object"},
    )
    res_agy = agy.parse_output(req_agy, agy_stream, None)
    assert res_agy.ok is True
    assert res_agy.parsed_json == {"status": "ok"}
    assert "https://example.org/agy-doc" in res_agy.observed_urls

    oc = OpenCodeAdapter()
    oc_stream = tmp_path / "oc.jsonl"
    oc_stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "webfetch",
                            "state": {"input": {"url": "https://example.org/oc-doc"}},
                        },
                    }
                ),
                json.dumps({"type": "text", "part": {"text": '{"items": [1, 2]}'}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    req_oc = WorkerRequest(
        worker_id="o1",
        role="subagent",
        prompt="p",
        work_dir=tmp_path,
        json_schema={"type": "object"},
    )
    res_oc = oc.parse_output(req_oc, oc_stream, None)
    assert res_oc.ok is True
    assert res_oc.parsed_json == {"items": [1, 2]}
    assert "https://example.org/oc-doc" in res_oc.observed_urls


def test_render_deterministic_report_repeated_citations_and_keep_ids() -> None:
    registry = {
        "S01": SourceRecord(
            source_id="S01",
            source_type="web",
            canonical_url="https://docs.example/a",
            final_url="https://docs.example/a",
            title="Kaynak A",
            fetch_status="fetched_ok",
            retrieved_at="2026-11-22T18:00:00+00:00",
        ),
        "S02": SourceRecord(
            source_id="S02",
            source_type="web",
            canonical_url="https://docs.example/b",
            final_url="https://docs.example/b",
            title="Kaynak B",
            fetch_status="fetch_blocked_snippet_only",
            seen_in_tool_trace=True,
            retrieved_at="2026-11-22T18:00:00+00:00",
        ),
    }
    draft = "Birinci cümle.[^S02] İkinci cümle.[^S01] Üçüncü cümle yine ilk kaynaktan.[^S02]"

    renumbered, cited_renum = render_deterministic_report(draft, registry, renumber=True)
    assert [c.source_id for c in cited_renum] == ["S02", "S01"]
    assert "Birinci cümle.[^1] İkinci cümle.[^2] Üçüncü cümle yine ilk kaynaktan.[^1]" in renumbered
    assert "Özet (Erişim Kısıtlı)" in renumbered

    kept, cited_kept = render_deterministic_report(draft, registry, renumber=False)
    assert [c.source_id for c in cited_kept] == ["S02", "S01"]
    assert "[^S02]: [Kaynak B](https://docs.example/b)" in kept
    assert "[^S01]: [Kaynak A](https://docs.example/a)" in kept

