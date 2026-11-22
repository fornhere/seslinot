"""SesliNot masaüstü katmanı (OSD, bildirimler, daemon, Omarchy eklentisi) birim testleri."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from seslinot.config import DesktopConfig, PathsConfig, SesliNotConfig, VaultConfig
from seslinot.db import SesliNotDB
from seslinot.desktop.daemon import (
    ROUTE_BACKOFF_SECS,
    ROUTE_MAX_ATTEMPTS,
    NetworkMonitor,
    ResearchExecutionResult,
    SesliNotDaemon,
)
from seslinot.desktop.notify import (
    NotificationAction,
    OmarchyNotifier,
    is_valid_obsidian_uri,
    make_obsidian_uri,
    validate_actions,
)
from seslinot.desktop.osd import OmarchyOSD
from seslinot.models import (
    DesktopState,
    JobStatus,
    ResearchJob,
    ResearchPhase,
    RouteDecision,
    VoiceNoteRecord,
    VoiceState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "integrations/omarchy-plugin/forn.seslinot"
BINDINGS_LUA = REPO_ROOT / "integrations/hyprland/bindings.lua"
SYSTEMD_SERVICE = REPO_ROOT / "integrations/systemd/seslinotd.service"


def _make_test_config(tmp_path: Path) -> SesliNotConfig:
    cfg = SesliNotConfig(
        vault=VaultConfig(path=tmp_path / "Hafıza", vault_name="Hafıza"),
        paths=PathsConfig(
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "runtime",
        ),
        desktop=DesktopConfig(
            enable_osd=False,
            enable_notifications=True,
            enable_bar_refresh=False,
        ),
    )
    cfg.paths.ensure_dirs()
    return cfg


def _seed_note(db: SesliNotDB, note_id: str = "note-1", title: str = "Test Notu") -> VoiceNoteRecord:
    note = VoiceNoteRecord(
        note_id=note_id,
        title=title,
        raw_transcript="Bu konuda derin bir araştırma yap.",
        vault_rel_path=f"sesli-notlar/{note_id}.md",
    )
    db.insert_note(note)
    return note


def _extract_qml_text_blocks(qml: str) -> list[str]:
    """QML kaynağındaki tüm `Text { ... }` bloklarını dengeli süslü parantez ile ayıklar."""
    blocks: list[str] = []
    idx = 0
    length = len(qml)
    while idx < length:
        pos = qml.find("Text", idx)
        if pos == -1:
            break
        before_ok = pos == 0 or not (qml[pos - 1].isalnum() or qml[pos - 1] in ("_", "."))
        after = pos + 4
        if not before_ok:
            idx = after
            continue
        while after < length and qml[after] in (" ", "\t", "\r", "\n"):
            after += 1
        if after >= length or qml[after] != "{":
            idx = pos + 4
            continue

        depth = 0
        start = pos
        cur = after
        while cur < length:
            ch = qml[cur]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(qml[start : cur + 1])
                    idx = cur + 1
                    break
            cur += 1
        else:
            break
    return blocks


# =====================================================================
# 1. UTF-8 Obsidian URI testleri
# =====================================================================


def test_make_obsidian_uri_utf8_and_md_stripping() -> None:
    uri = make_obsidian_uri("Hafıza", "araştırmalar/2026-11-22-yerel gpu çalışma.md")
    assert uri.startswith("obsidian://open?vault=Haf%C4%B1za&file=")
    assert "Haf%C4%B1za" in uri
    assert "ara%C5%9Ft%C4%B1rmalar" in uri
    assert "%20" in uri
    assert "+" not in uri
    assert not uri.endswith(".md")
    assert not uri.endswith(".MD")
    assert is_valid_obsidian_uri(uri)


def test_make_obsidian_uri_strips_uppercase_md_and_leading_slash() -> None:
    uri = make_obsidian_uri("Hafıza", "/sesli-notlar/Örnek Not.MD")
    assert uri == "obsidian://open?vault=Haf%C4%B1za&file=sesli-notlar%2F%C3%96rnek%20Not"
    assert is_valid_obsidian_uri(uri)


def test_obsidian_uri_validation_rejects_unsafe_inputs() -> None:
    with pytest.raises(ValueError):
        make_obsidian_uri("", "not.md")
    with pytest.raises(ValueError):
        make_obsidian_uri("Hafıza", "dosya\ninjection.md")
    assert not is_valid_obsidian_uri("https://example.com")
    assert not is_valid_obsidian_uri("obsidian://open?vault=Hafıza&file=boşluk var")
    assert not is_valid_obsidian_uri("obsidian://open?vault=a\nb")


# =====================================================================
# 2. OmarchyNotifier güvenlik değişmezi & taşıma testleri
# =====================================================================


def test_validate_actions_enforces_max_three_and_safe_first_action() -> None:
    cancel = NotificationAction(label="İptal Et", argv=["seslinot-ctl", "cancel", "j1"])
    view = NotificationAction(label="Durumu Gör", argv=["omarchy-shell", "-q", "forn.seslinot", "open"])
    extra1 = NotificationAction(label="Aksiyon 2", argv=["echo", "2"])
    extra2 = NotificationAction(label="Aksiyon 3", argv=["echo", "3"])

    # Aksiyon #0 yıkıcıysa güvenli aksiyon ilk sıraya alınır
    reordered = validate_actions([cancel, view])
    assert reordered[0].label == "Durumu Gör"
    assert reordered[1].label == "İptal Et"

    # Yalnızca yıkıcı aksiyon varsa güvenli yeniden sıralama mümkün değil -> ValueError
    with pytest.raises(ValueError):
        validate_actions([cancel])

    # strict=True iken ilk sıradaki yıkıcı aksiyon doğrudan reddedilir
    with pytest.raises(ValueError):
        validate_actions([cancel, view], strict=True)

    # En fazla 3 aksiyon
    assert len(validate_actions([view, extra1, cancel])) == 3
    with pytest.raises(ValueError):
        validate_actions([view, extra1, extra2, cancel])

    # Geçersiz argv (lider '-' veya boş)
    with pytest.raises(ValueError):
        validate_actions([NotificationAction(label="Kötü", argv=["-rf", "/"])])


def test_notifier_high_level_helpers_and_invariants(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    notifier = OmarchyNotifier(cfg)
    calls: list[dict[str, Any]] = []

    def fake_send(summary: str, body: str = "", **kwargs: Any) -> int:
        # Gerçek validate_actions kuralının her yüksek seviye çağrıda sağlandığını doğrula
        actions = validate_actions(kwargs.get("actions", ()), strict=True)
        calls.append({"summary": summary, "body": body, "actions": actions, **kwargs})
        return len(calls) + 100

    notifier.send = fake_send  # type: ignore[method-assign]

    started_id = notifier.notify_research_started("job-1", "Yerel Whisper optimizasyonu")
    assert started_id == 101
    assert [a.label for a in calls[-1]["actions"]] == ["Durumu Gör", "İptal Et"]
    assert calls[-1]["transient"] is True

    notifier.notify_research_phase(started_id, "job-1", "Yerel Whisper optimizasyonu", "Kaynaklar doğrulanıyor")
    assert calls[-1]["replaces_id"] == started_id
    assert calls[-1]["actions"][0].label == "Durumu Gör"

    notifier.notify_research_confirm("job-2", "Rust vs Python", 0.72)
    assert calls[-1]["timeout_ms"] == 30000
    assert [a.label for a in calls[-1]["actions"]] == ["Araştırmayı Başlat", "Sadece Not Kalsın"]

    uri = make_obsidian_uri("Hafıza", "araştırmalar/test-rapor.md")
    notifier.notify_research_completed(started_id, "Yerel Whisper optimizasyonu", uri, 7)
    assert calls[-1]["replaces_id"] == started_id
    assert calls[-1]["transient"] is False
    assert calls[-1]["actions"][0].label == "Obsidian'da Aç"
    assert calls[-1]["actions"][0].argv == ["xdg-open", uri]

    notifier.notify_research_cancelled(started_id, "Yerel Whisper optimizasyonu")
    assert calls[-1]["replaces_id"] == started_id

    notifier.notify_research_failed(started_id, "Yerel Whisper optimizasyonu", "Zaman aşımı")
    assert calls[-1]["replaces_id"] == started_id
    assert calls[-1]["urgency"] == "critical"

    notifier.notify_note_saved("Kısa Not", uri, duration_secs=14)
    assert calls[-1]["actions"][0].argv == ["xdg-open", uri]


def test_notifier_dbus_primary_and_cli_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    notifier = OmarchyNotifier(cfg)

    # 1) Primary D-Bus yolu (sahte dbus modülü ile)
    recorded_dbus: list[tuple[Any, ...]] = []

    class FakeIface:
        def Notify(self, *args: Any) -> int:
            recorded_dbus.append(args)
            return 777

    class FakeSessionBus:
        def get_object(self, service: str, path: str) -> object:
            return (service, path)

    fake_dbus = types.SimpleNamespace(
        SessionBus=FakeSessionBus,
        Interface=lambda proxy, iface: FakeIface(),
        String=str,
        UInt32=int,
        Int32=int,
        Byte=int,
        Boolean=bool,
        Array=lambda items, signature="": list(items),
        Dictionary=lambda d, signature="": dict(d),
    )
    monkeypatch.setitem(sys.modules, "dbus", fake_dbus)

    uri = make_obsidian_uri("Hafıza", "araştırmalar/rapor.md")
    nid = notifier.notify_research_completed(42, "Konu", uri, 5)
    assert nid == 777
    assert len(recorded_dbus) == 1
    app_name, replaces_id, _icon, summary, _body, _acts, hints, _timeout = recorded_dbus[0]
    assert app_name == "SesliNot"
    assert replaces_id == 42
    assert summary == "Araştırma kasaya kaydedildi"
    assert hints["transient"] is False
    assert "omarchy-glyph" in hints
    parsed_actions = json.loads(hints["omarchy-actions"])
    assert parsed_actions[0]["label"] == "Obsidian'da Aç"
    assert json.loads(hints["omarchy-exec-argv"]) == ["xdg-open", uri]

    # 2) dbus yokken omarchy-notification-send CLI yedeği
    monkeypatch.setitem(sys.modules, "dbus", None)  # type: ignore[arg-type]
    ran_cmds: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "omarchy-notification-send":
            return "/usr/share/omarchy/bin/omarchy-notification-send"
        return None

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="555\n", stderr="")

    monkeypatch.setattr("seslinot.desktop.notify.shutil.which", fake_which)
    monkeypatch.setattr("seslinot.desktop.notify.subprocess.run", fake_run)

    cli_id = notifier.notify_research_completed(42, "Konu", uri, 3)
    assert cli_id == 555
    assert ran_cmds[-1][0].endswith("omarchy-notification-send")
    assert "-r" in ran_cmds[-1] and "42" in ran_cmds[-1]
    assert "--transient" not in ran_cmds[-1]
    assert "--action" in ran_cmds[-1]

    # 3) omarchy-notification-send de yokken generic notify-send yedeği
    monkeypatch.setattr(
        "seslinot.desktop.notify.shutil.which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    fallback_id = notifier.notify_research_cancelled(42, "Konu")
    assert fallback_id == 555
    assert ran_cmds[-1][0] == "/usr/bin/notify-send"
    assert "--" in ran_cmds[-1]


# =====================================================================
# 3. OmarchyOSD atomik state.json yazımı & IPC testleri
# =====================================================================


def test_osd_atomic_state_write_and_bar_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    cfg.desktop.enable_osd = True
    cfg.desktop.enable_bar_refresh = True

    popen_cmds: list[list[str]] = []

    class DummyPopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            popen_cmds.append(cmd)

    fsync_calls: list[int] = []
    replace_calls: list[tuple[Path, Path]] = []
    real_fsync = __import__("os").fsync
    real_replace = __import__("os").replace

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any) -> None:
        replace_calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr("seslinot.desktop.osd.shutil.which", lambda n: "/usr/share/omarchy/bin/omarchy-shell")
    monkeypatch.setattr("seslinot.desktop.osd.subprocess.Popen", DummyPopen)
    monkeypatch.setattr("seslinot.desktop.osd.os.fsync", spy_fsync)
    monkeypatch.setattr("seslinot.desktop.osd.os.replace", spy_replace)

    osd = OmarchyOSD(cfg)
    state = DesktopState(
        voiceState=VoiceState.RECORDING.value,
        recordingStartedAt=1763830000,
        queuedOfflineCount=2,
    )
    out_path = osd.write_desktop_state(state)

    assert out_path == cfg.paths.state_json_path
    assert out_path.is_file()
    assert len(fsync_calls) == 1
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == out_path
    # Artık geçici dosya kalmamalı
    assert list(out_path.parent.glob("*.tmp")) == []

    loaded = DesktopState.model_validate_json(out_path.read_text(encoding="utf-8"))
    assert loaded == state
    assert popen_cmds[-1] == [
        "/usr/share/omarchy/bin/omarchy-shell",
        "-q",
        "forn.seslinot.state",
        "refresh",
    ]

    assert osd.show("Kayıt dinleniyor", icon="microphone", duration_ms=0) is True
    assert popen_cmds[-1][:4] == ["/usr/share/omarchy/bin/omarchy-shell", "-q", "osd", "show"]
    osd_payload = json.loads(popen_cmds[-1][4])
    assert osd_payload == {"icon": "microphone", "message": "Kayıt dinleniyor", "duration": 0}

    assert osd.close() is True
    assert popen_cmds[-1] == ["/usr/share/omarchy/bin/omarchy-shell", "-q", "osd", "close"]


# =====================================================================
# 4. Omarchy Eklenti Şeması, BarWidget.qml ve Şablon Doğrulaması
# =====================================================================


def test_omarchy_plugin_manifest_and_bar_widget_security() -> None:
    manifest_path = PLUGIN_DIR / "manifest.json"
    qml_path = PLUGIN_DIR / "BarWidget.qml"
    assert manifest_path.is_file()
    assert qml_path.is_file()

    # Eklenti klasöründe sembolik bağ yasak
    for entry in PLUGIN_DIR.rglob("*"):
        assert not entry.is_symlink(), f"Sembolik bağ yasak: {entry}"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert type(manifest["schemaVersion"]) is int
    assert manifest["schemaVersion"] == 1
    assert manifest["id"] == "forn.seslinot"
    assert "bar-widget" in manifest["kinds"]
    assert manifest["keepLoaded"] is True
    assert manifest["entryPoints"]["barWidget"] == "BarWidget.qml"
    assert manifest["barWidget"]["defaultSection"] == "centerEnd"

    # Omarchy CLI kuruluysa resmi şema doğrulayıcısını çalıştır
    omarchy_bin = shutil.which("omarchy")
    if omarchy_bin:
        proc = subprocess.run(
            [omarchy_bin, "plugin", "validate", str(PLUGIN_DIR)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"omarchy plugin validate başarısız: {proc.stderr}"

    # BarWidget.qml yapısal ve güvenlik kontrolleri
    qml = qml_path.read_text(encoding="utf-8")
    assert "Panel {" in qml
    assert "KeyboardPanel {" in qml
    assert "PanelKeyCatcher {" in qml
    assert "FileView {" in qml
    assert 'target: "forn.seslinot.state"' in qml
    assert "function refresh()" in qml
    assert "Style.duration(" in qml
    assert 'uri.indexOf("obsidian://open?") === 0' in qml
    assert 'Quickshell.execDetached(["seslinot-ctl"' in qml
    assert 'Quickshell.execDetached(["xdg-open", uri])' in qml
    assert "sh -c" not in qml

    # Her Text { ... } bloğunda textFormat: Text.PlainText zorunlu!
    text_blocks = _extract_qml_text_blocks(qml)
    assert len(text_blocks) >= 8
    for idx, block in enumerate(text_blocks):
        assert "textFormat: Text.PlainText" in block, (
            f"BarWidget.qml içindeki {idx}. Text bloğunda textFormat: Text.PlainText eksik:\n{block}"
        )


def test_hyprland_bindings_and_systemd_unit_templates() -> None:
    lua = BINDINGS_LUA.read_text(encoding="utf-8")
    assert "SUPER + N" in lua
    assert "SUPER + ALT + Z" in lua
    assert "o.bind_hold(" in lua
    assert "SUPER + ALT + N" in lua
    assert "SUPER + SHIFT + ESCAPE" in lua
    assert "SUPER + SHIFT + R" in lua

    unit = SYSTEMD_SERVICE.read_text(encoding="utf-8")
    assert "seslinotd" in unit
    assert "After=graphical-session.target" in unit
    assert "Restart=on-failure" in unit


# =====================================================================
# 5. NetworkMonitor & SesliNotDaemon testleri
# =====================================================================


def test_network_monitor_nmcli_busctl_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = NetworkMonitor()

    # nmcli -> full => True
    monkeypatch.setattr(
        "seslinot.desktop.daemon.shutil.which",
        lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
    )
    monkeypatch.setattr(
        "seslinot.desktop.daemon.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="full\n", stderr=""),
    )
    assert monitor.is_online() is True

    # nmcli -> none => False
    monkeypatch.setattr(
        "seslinot.desktop.daemon.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="none\n", stderr=""),
    )
    assert monitor.is_online() is False

    # nmcli yok, busctl -> u 4 => True; u 1 => False
    monkeypatch.setattr(
        "seslinot.desktop.daemon.shutil.which",
        lambda name: "/usr/bin/busctl" if name == "busctl" else None,
    )
    monkeypatch.setattr(
        "seslinot.desktop.daemon.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="u 4\n", stderr=""),
    )
    assert monitor.is_online() is True

    monkeypatch.setattr(
        "seslinot.desktop.daemon.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="u 1\n", stderr=""),
    )
    assert monitor.is_online() is False

    # İkisi de yoksa güvenli varsayılan True
    monkeypatch.setattr("seslinot.desktop.daemon.shutil.which", lambda name: None)
    assert monitor.is_online() is True


class RecordingNotifier(OmarchyNotifier):
    """Testlerde gerçek bildirim göndermeden tüm çağrıları kaydeden yardımcı."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        super().__init__(cfg)
        self.events: list[dict[str, Any]] = []
        self._counter = 1000

    def send(self, summary: str, body: str = "", **kwargs: Any) -> int:
        actions = validate_actions(kwargs.get("actions", ()))
        replaces_id = int(kwargs.get("replaces_id", 0) or 0)
        notif_id = replaces_id if replaces_id > 0 else self._next_id()
        self.events.append(
            {
                "id": notif_id,
                "summary": summary,
                "body": body,
                "actions": actions,
                **kwargs,
            }
        )
        return notif_id

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter


@pytest.mark.asyncio
async def test_daemon_run_once_route_and_research_lifecycle(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    db = SesliNotDB(cfg.paths.db_path)
    osd = OmarchyOSD(cfg)
    notifier = RecordingNotifier(cfg)
    _seed_note(db, "note-auto", "Otomatik Araştırma Notu")
    _seed_note(db, "note-confirm", "Onaylı Araştırma Notu")

    db.enqueue_job(
        ResearchJob(
            job_id="job-auto",
            note_id="note-auto",
            topic="Wayland ses yakalama",
            raw_prompt="Wayland PipeWire ses yakalama en iyi pratiklerini araştır",
            status=JobStatus.PENDING_ROUTE,
        )
    )
    db.enqueue_job(
        ResearchJob(
            job_id="job-confirm",
            note_id="note-confirm",
            topic="Quickshell bellek tüketimi",
            raw_prompt="Quickshell bellek tüketimi nasıl acaba",
            status=JobStatus.PENDING_ROUTE,
        )
    )

    async def fake_router(job: ResearchJob) -> RouteDecision:
        if job.job_id == "job-auto":
            return RouteDecision(
                is_research=True,
                confidence=0.92,
                intent_type="imperative",
                research_topic=job.topic,
                reason="Açık araştırma emri",
            )
        return RouteDecision(
            is_research=True,
            confidence=0.68,
            intent_type="question_implied",
            research_topic=job.topic,
            reason="Orta güven düzeyi",
        )

    async def fake_research(job: ResearchJob, on_phase: Any) -> ResearchExecutionResult:
        await on_phase(ResearchPhase.GATHERING, "3 alt-ajan kaynak topluyor")
        await on_phase(ResearchPhase.SYNTHESIZING, "Rapor yazılıyor")
        return ResearchExecutionResult(
            report_rel_path="araştırmalar/2026-11-22-wayland-ses.md",
            source_count=6,
            session_dir=str(tmp_path / "sessions" / job.job_id),
        )

    daemon = SesliNotDaemon(
        cfg,
        db=db,
        osd=osd,
        notifier=notifier,
        route_handler=fake_router,
        research_handler=fake_research,
        network_checker=lambda: True,
    )

    tick = await daemon.run_once()
    assert tick.online is True
    assert tick.routed_jobs == 2
    assert tick.dispatched_jobs == 1

    await daemon.wait_for_active_tasks(timeout=5.0)

    # Otomatik iş tamamlandı ve nota bağlandı
    auto_job = db.get_job("job-auto")
    assert auto_job is not None
    assert auto_job.status == JobStatus.SUCCEEDED
    assert auto_job.phase == ResearchPhase.COMPLETED
    assert auto_job.source_count == 6
    assert auto_job.obsidian_uri.startswith("obsidian://open?vault=Haf%C4%B1za&file=ara%C5%9Ft%C4%B1rmalar")

    linked_note = db.get_note("note-auto")
    assert linked_note is not None
    assert linked_note.related_research == "araştırmalar/2026-11-22-wayland-ses.md"
    assert linked_note.status.value == "araştırıldı"

    # Orta güven işi onay bekliyor
    confirm_job = db.get_job("job-confirm")
    assert confirm_job is not None
    assert confirm_job.status == JobStatus.PENDING_CONFIRM

    # Bildirim yaşam döngüsü doğrulaması
    completed_notifs = [e for e in notifier.events if e["summary"] == "Araştırma kasaya kaydedildi"]
    assert len(completed_notifs) == 1
    assert completed_notifs[0]["transient"] is False
    assert completed_notifs[0]["replaces_id"] == auto_job.notif_id

    # state.json doğrulaması
    desktop_state = DesktopState.model_validate_json(
        cfg.paths.state_json_path.read_text(encoding="utf-8")
    )
    assert len(desktop_state.pendingConfirm) == 1
    assert desktop_state.pendingConfirm[0].id == "job-confirm"
    assert len(desktop_state.recentReports) == 1
    assert desktop_state.recentReports[0].id == "job-auto"
    assert desktop_state.recentReports[0].sourceCount == 6


@pytest.mark.asyncio
async def test_daemon_route_exponential_backoff_and_max_attempts(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    db = SesliNotDB(cfg.paths.db_path)
    osd = OmarchyOSD(cfg)
    notifier = RecordingNotifier(cfg)
    _seed_note(db, "note-retry")

    db.enqueue_job(
        ResearchJob(
            job_id="job-retry",
            note_id="note-retry",
            topic="Geçici hata testi",
            raw_prompt="Bunu araştır",
            status=JobStatus.PENDING_ROUTE,
        )
    )

    def failing_router(job: ResearchJob) -> RouteDecision:
        raise TimeoutError("Router zaman aşımı")

    daemon = SesliNotDaemon(
        cfg,
        db=db,
        osd=osd,
        notifier=notifier,
        route_handler=failing_router,
        network_checker=lambda: True,
    )

    for attempt in range(1, ROUTE_MAX_ATTEMPTS):
        await daemon.run_once()
        job = db.get_job("job-retry")
        assert job is not None
        assert job.status == JobStatus.PENDING_ROUTE
        assert job.next_retry_at is not None
        # Beklenen backoff kaydı olaylarda doğrulanır
        events = db.list_events("job-retry")
        expected_backoff = ROUTE_BACKOFF_SECS[min(attempt - 1, len(ROUTE_BACKOFF_SECS) - 1)]
        assert events[-1]["payload"]["backoff_secs"] == expected_backoff
        # Sonraki test turu için retry zamanını geçmişe çek
        db.update_job_status("job-retry", JobStatus.PENDING_ROUTE, next_retry_at="2000-01-01T00:00:00+00:00")

    # 5. deneme kalıcı FAILED durumuna geçer
    await daemon.run_once()
    final_job = db.get_job("job-retry")
    assert final_job is not None
    assert final_job.status == JobStatus.FAILED
    assert final_job.finished_at is not None


@pytest.mark.asyncio
async def test_daemon_offline_to_online_promotion(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    db = SesliNotDB(cfg.paths.db_path)
    osd = OmarchyOSD(cfg)
    notifier = RecordingNotifier(cfg)
    _seed_note(db, "note-fresh")
    _seed_note(db, "note-stale")

    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0).isoformat()
    db.enqueue_job(
        ResearchJob(
            job_id="job-fresh",
            note_id="note-fresh",
            topic="Taze çevrimdışı konu",
            raw_prompt="Araştır",
            status=JobStatus.QUEUED_OFFLINE,
        )
    )
    db.enqueue_job(
        ResearchJob(
            job_id="job-stale",
            note_id="note-stale",
            topic="Bayat çevrimdışı konu",
            raw_prompt="Araştır",
            status=JobStatus.QUEUED_OFFLINE,
            created_at=stale_ts,
        )
    )

    online_flag = {"value": False}
    dispatched_ids: list[str] = []

    async def fake_research(job: ResearchJob, on_phase: Any) -> ResearchExecutionResult:
        dispatched_ids.append(job.job_id)
        return ResearchExecutionResult(report_rel_path="araştırmalar/taze.md", source_count=2)

    daemon = SesliNotDaemon(
        cfg,
        db=db,
        osd=osd,
        notifier=notifier,
        research_handler=fake_research,
        network_checker=lambda: online_flag["value"],
    )

    # Çevrimdışı tur: hiçbir şey yükselmemeli veya dağıtılmamalı
    tick1 = await daemon.run_once()
    assert tick1.online is False
    assert tick1.promoted_offline == 0
    assert tick1.dispatched_jobs == 0
    assert db.get_job("job-fresh").status == JobStatus.QUEUED_OFFLINE  # type: ignore[union-attr]

    state_offline = DesktopState.model_validate_json(
        cfg.paths.state_json_path.read_text(encoding="utf-8")
    )
    assert state_offline.queuedOfflineCount == 2

    # Çevrimiçi geçiş: taze iş QUEUED -> RUNNING -> SUCCEEDED; bayat iş PENDING_CONFIRM
    online_flag["value"] = True
    tick2 = await daemon.run_once()
    assert tick2.online is True
    assert tick2.promoted_offline == 1
    assert tick2.staled_offline == 1
    assert tick2.dispatched_jobs == 1

    await daemon.wait_for_active_tasks(timeout=5.0)
    assert dispatched_ids == ["job-fresh"]
    assert db.get_job("job-fresh").status == JobStatus.SUCCEEDED  # type: ignore[union-attr]
    assert db.get_job("job-stale").status == JobStatus.PENDING_CONFIRM  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_daemon_confirm_dismiss_and_cancel_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    db = SesliNotDB(cfg.paths.db_path)
    osd = OmarchyOSD(cfg)
    notifier = RecordingNotifier(cfg)
    _seed_note(db, "note-c1")
    _seed_note(db, "note-c2")
    _seed_note(db, "note-run")

    db.enqueue_job(
        ResearchJob(
            job_id="job-c1",
            note_id="note-c1",
            topic="Onaylanacak konu",
            raw_prompt="Araştır",
            status=JobStatus.PENDING_CONFIRM,
            confidence=0.71,
        )
    )
    db.enqueue_job(
        ResearchJob(
            job_id="job-c2",
            note_id="note-c2",
            topic="Reddedilecek konu",
            raw_prompt="Not kalsın",
            status=JobStatus.PENDING_CONFIRM,
            confidence=0.60,
        )
    )
    db.enqueue_job(
        ResearchJob(
            job_id="job-run",
            note_id="note-run",
            topic="İptal edilecek çalışma",
            raw_prompt="Uzun araştırma",
            status=JobStatus.QUEUED,
            unit_name="seslinot-job-run.service",
        )
    )

    stopped_units: list[list[str]] = []
    monkeypatch.setattr(
        "seslinot.desktop.daemon.shutil.which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(
        "seslinot.desktop.daemon.subprocess.run",
        lambda cmd, **kw: (stopped_units.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    started_evt = asyncio.Event()
    cancelled_flag = {"hit": False}

    async def slow_research(job: ResearchJob, on_phase: Any) -> ResearchExecutionResult:
        started_evt.set()
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            cancelled_flag["hit"] = True
            raise
        return ResearchExecutionResult()

    online_flag = {"value": False}
    daemon = SesliNotDaemon(
        cfg,
        db=db,
        osd=osd,
        notifier=notifier,
        research_handler=slow_research,
        network_checker=lambda: online_flag["value"],
    )

    # Çevrimdışıyken confirm -> QUEUED_OFFLINE
    assert daemon.confirm_job("job-c1") is True
    assert db.get_job("job-c1").status == JobStatus.QUEUED_OFFLINE  # type: ignore[union-attr]
    # Tekrar confirm edilemez
    assert daemon.confirm_job("job-c1") is False

    # Dismiss -> DISMISSED
    assert daemon.dismiss_job("job-c2") is True
    assert db.get_job("job-c2").status == JobStatus.DISMISSED  # type: ignore[union-attr]
    assert daemon.dismiss_job("job-c2") is False

    # Çevrimiçi olup job-run'ı başlat (job-c1'i de durdurmak için sadece job-run'ı test edelim)
    db.update_job_status("job-c1", JobStatus.DISMISSED, finished=True)
    online_flag["value"] = True
    await daemon.run_once()
    await asyncio.wait_for(started_evt.wait(), timeout=2.0)

    # Çalışan işi iptal et: systemctl stop + asyncio task cancel + DB CANCELLED + state.json
    assert daemon.cancel_job("job-run") is True
    await daemon.wait_for_active_tasks(timeout=2.0)

    assert cancelled_flag["hit"] is True
    assert ["/usr/bin/systemctl", "--user", "stop", "seslinot-job-run.service"] in stopped_units
    cancelled_job = db.get_job("job-run")
    assert cancelled_job is not None
    assert cancelled_job.status == JobStatus.CANCELLED
    assert cancelled_job.finished_at is not None

    state_after = DesktopState.model_validate_json(
        cfg.paths.state_json_path.read_text(encoding="utf-8")
    )
    assert state_after.activeSessions == []
    assert any(e["summary"] == "Araştırma iptal edildi" for e in notifier.events)


def test_notify_research_ready_for_approval_actions_and_safety(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    notifier = RecordingNotifier(cfg)
    preview_file = tmp_path / "preview.md"
    preview_file.write_text("# Önizleme\n", encoding="utf-8")

    # 1) Önizleme dosyası varken Aksiyon #0 güvenli 'Önizle' olmalı, toplam 3 aksiyon
    notifier.notify_research_ready_for_approval(
        notif_id=12,
        job_id="res-approval-1",
        topic="SQLite WAL Optimizasyonu",
        preview_path=str(preview_file),
        source_count=9,
        confidence="yüksek",
    )
    ev = notifier.events[-1]
    assert ev["summary"] == "Araştırma hazır — Kaydedilsin mi?"
    assert ev["transient"] is False
    assert ev["replaces_id"] == 12
    assert len(ev["actions"]) == 3
    assert ev["actions"][0].label == "Önizle"
    assert ev["actions"][0].argv == ["xdg-open", str(preview_file)]
    assert ev["actions"][1].label == "Kasaya Kaydet"
    assert ev["actions"][1].argv == ["seslinot-ctl", "approve-report", "res-approval-1"]
    assert ev["actions"][2].label == "Reddet"
    assert ev["actions"][2].argv == ["seslinot-ctl", "reject-report", "res-approval-1"]
    # validate_actions sorunsuz geçmeli
    validate_actions(ev["actions"])

    # 2) Önizleme dosyası yoksa Aksiyon #0 güvenli 'Paneli Aç' olmalı
    notifier.notify_research_ready_for_approval(
        notif_id=13,
        job_id="res-approval-2",
        topic="Kayıp Önizleme",
        preview_path=str(tmp_path / "yok.md"),
        source_count=4,
        confidence="orta",
    )
    ev2 = notifier.events[-1]
    assert ev2["actions"][0].label == "Paneli Aç"
    assert ev2["actions"][1].label == "Kasaya Kaydet"
    assert ev2["actions"][2].label == "Reddet"

    # 3) reject-report asla Aksiyon #0 olamamalı
    with pytest.raises(ValueError, match="ilk aksiyon güvenli"):
        validate_actions(
            [NotificationAction(label="Vazgeç", argv=["seslinot-ctl", "reject-report", "res-1"])]
        )

    # 4) Reddetme bildirimi transient olmalı
    notifier.notify_research_rejected(12, "SQLite WAL Optimizasyonu")
    ev_rej = notifier.events[-1]
    assert ev_rej["summary"] == "Rapor kasaya kaydedilmedi"
    assert ev_rej["transient"] is True

