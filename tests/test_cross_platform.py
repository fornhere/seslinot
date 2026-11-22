"""Windows/macOS/Linux çapraz platform özellikleri testleri: Cloud STT, DirectApiAdapter, bildirim ve ses backendleri."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from seslinot.audio.capture import AudioCaptureController
from seslinot.audio.stt import resolve_stt_target
from seslinot.config import AudioConfig, DesktopConfig, PathsConfig, SesliNotConfig, VaultConfig
from seslinot.desktop.notify import DesktopNotifier
from seslinot.research.adapters import DirectApiAdapter, WorkerRequest, get_adapter
from seslinot.research.adapters.direct_api import parse_ddg_html_results
from seslinot.research.runner import ProcessRunner


def _make_cfg(tmp_path: Path) -> SesliNotConfig:
    return SesliNotConfig(
        vault=VaultConfig(path=tmp_path / "Vault", vault_name="Vault"),
        paths=PathsConfig(
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "runtime",
        ),
        desktop=DesktopConfig(enable_osd=False, enable_bar_refresh=False),
    )


def test_resolve_stt_target_cloud_providers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_123")
    cfg.audio = AudioConfig(stt_provider="groq")
    endpoint, model, headers = resolve_stt_target(cfg)
    assert endpoint == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert model == "whisper-large-v3-turbo"
    assert headers == {"Authorization": "Bearer gsk_test_123"}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-456")
    cfg.audio = AudioConfig(stt_provider="openai")
    endpoint2, model2, headers2 = resolve_stt_target(cfg)
    assert endpoint2 == "https://api.openai.com/v1/audio/transcriptions"
    assert model2 == "whisper-1"
    assert headers2 == {"Authorization": "Bearer sk-test-456"}


def test_capture_backend_resolution_non_linux_uses_sounddevice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _make_cfg(tmp_path)
    ctrl = AudioCaptureController(cfg)

    monkeypatch.setattr("seslinot.audio.capture.current_platform", lambda: "windows")
    assert ctrl._resolve_backend() == "sounddevice"

    monkeypatch.setattr("seslinot.audio.capture.current_platform", lambda: "macos")
    assert ctrl._resolve_backend() == "sounddevice"

    monkeypatch.setattr("seslinot.audio.capture.current_platform", lambda: "linux")
    monkeypatch.setattr("seslinot.audio.capture.shutil.which", lambda name: None)
    assert ctrl._resolve_backend() == "sounddevice"


def test_desktop_notifier_macos_and_windows_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _make_cfg(tmp_path)
    notifier = DesktopNotifier(cfg)
    cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("seslinot.desktop.notify.subprocess.run", fake_run)

    # macOS osascript
    monkeypatch.setattr("seslinot.platform.os_compat.current_platform", lambda *a, **k: "macos")
    monkeypatch.setattr(
        "seslinot.desktop.notify.shutil.which",
        lambda name: "/usr/bin/osascript" if name == "osascript" else None,
    )
    assert notifier.send("Başlık", "Gövde") == 1
    assert cmds[-1][0] == "/usr/bin/osascript"
    assert "display notification" in cmds[-1][2]

    # Windows PowerShell WinRT Toast
    monkeypatch.setattr("seslinot.platform.os_compat.current_platform", lambda *a, **k: "windows")
    monkeypatch.setattr(
        "seslinot.desktop.notify.shutil.which",
        lambda name: "C:/Windows/System32/powershell.exe" if name == "powershell" else None,
    )
    assert notifier.send("Rapor Hazır", "A < B & C") == 1
    assert "powershell" in cmds[-1][0].lower()
    assert "A &lt; B &amp; C" in cmds[-1][-1]


def test_parse_ddg_html_results_unwraps_redirect_urls() -> None:
    sample_html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsqlite.org%2Fwal.html&amp;rut=abc">SQLite <b>WAL</b></a>
      <a class="result__snippet">Write-Ahead Logging increases concurrency.</a>
    </div>
    """
    hits = parse_ddg_html_results(sample_html)
    assert len(hits) == 1
    assert hits[0]["url"] == "https://sqlite.org/wal.html"
    assert hits[0]["title"] == "SQLite WAL"
    assert "concurrency" in hits[0]["snippet"]


@pytest.mark.asyncio
async def test_direct_api_adapter_via_process_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-mock-key-123")
    assert isinstance(get_adapter("api"), DirectApiAdapter)

    def _handler(request: httpx.Request) -> httpx.Response:
        if "duckduckgo.com" in str(request.url):
            body = (
                '<a class="result__a" href="https://docs.example.org/wal">WAL Docs</a>'
                '<a class="result__snippet">WAL allows concurrent readers.</a>'
            )
            return httpx.Response(200, text=body)
        if request.url.path.endswith("/chat/completions"):
            content = json.dumps(
                {
                    "worker_id": "w1",
                    "subtopic": "WAL",
                    "status": "completed",
                    "gaps_or_blockers": [],
                    "findings": [
                        {
                            "claim_id": "c1",
                            "claim_tr": "WAL eşzamanlı okuma sağlar.",
                            "exact_quote": "WAL allows concurrent readers.",
                            "url": "https://docs.example.org/wal",
                            "title": "WAL Docs",
                            "source_type": "web",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as mock_http:
        adapter = DirectApiAdapter(provider="openai", search_provider="ddg", http_client=mock_http)
        runner = ProcessRunner(use_systemd_run=False)
        req = WorkerRequest(
            worker_id="w1",
            role="subagent",
            prompt="- SQLite WAL concurrency\n",
            work_dir=tmp_path / "w1",
            json_schema={"type": "object"},
            allow_web=True,
        )
        res = await runner.run(adapter, req)
        assert res.ok is True
        assert res.backend == "api"
        assert "https://docs.example.org/wal" in res.observed_urls
        assert res.parsed_json is not None
        assert res.parsed_json["findings"][0]["url"] == "https://docs.example.org/wal"
