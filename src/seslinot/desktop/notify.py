"""Omarchy masaüstü bildirimleri ve UTF-8 Obsidian URI yardımcıları."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from seslinot.config import SesliNotConfig

logger = logging.getLogger(__name__)

MAX_NOTIFICATION_ACTIONS = 3
MAX_ACTION_LABEL_LEN = 256
MAX_EXEC_ARGV_LEN = 32
MAX_ARG_LEN = 4096

UrgencyLevel = Literal["low", "normal", "critical"]

_URGENCY_TO_BYTE: dict[UrgencyLevel, int] = {
    "low": 0,
    "normal": 1,
    "critical": 2,
}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_DESTRUCTIVE_LABEL_TOKENS: tuple[str, ...] = (
    "iptal",
    "cancel",
    "sil",
    "delete",
    "durdur",
    "stop",
    "reddet",
    "dismiss",
)

_DESTRUCTIVE_ARGV_SUBCOMMANDS: frozenset[str] = frozenset(
    {"cancel", "dismiss", "delete", "stop", "kill", "remove", "reject-report"}
)

_OMARCHY_KNOWN_FLAGS: frozenset[str] = frozenset(
    {
        "-g",
        "--glyph",
        "-u",
        "--urgency",
        "--app-name",
        "-i",
        "--icon",
        "-t",
        "--expire-time",
        "--image",
        "-r",
        "--replace-id",
        "-p",
        "--print-id",
        "--transient",
        "--exec",
        "--action",
        "--with",
    }
)


def _normalize_tr_lower(text: str) -> str:
    """Türkçe İ/I harflerini bozmadan küçük harfe indirger."""
    return text.replace("İ", "i").replace("I", "ı").lower().strip()


def make_obsidian_uri(vault_name: str, rel_file_path: str) -> str:
    """Obsidian için UTF-8 percent-encoded `obsidian://open?...` URI üretir.

    - Hem `vault` (`Hafıza` -> `Haf%C4%B1za`) hem `file` UTF-8 olarak kodlanır.
    - `rel_file_path` sonundaki `.md` uzantısı (büyük/küçük harf duyarsız) silinir.
    """
    vault_clean = vault_name.strip()
    if not vault_clean or _CONTROL_CHAR_RE.search(vault_clean):
        raise ValueError("Geçersiz vault_name değeri")

    file_clean = rel_file_path.replace("\\", "/").strip().lstrip("/")
    if not file_clean or _CONTROL_CHAR_RE.search(file_clean):
        raise ValueError("Geçersiz rel_file_path değeri")

    if file_clean.lower().endswith(".md"):
        file_clean = file_clean[:-3]
    file_clean = file_clean.rstrip("/")
    if not file_clean:
        raise ValueError("rel_file_path dosya adı içermelidir")

    encoded_vault = quote(vault_clean, safe="", encoding="utf-8")
    encoded_file = quote(file_clean, safe="", encoding="utf-8")
    return f"obsidian://open?vault={encoded_vault}&file={encoded_file}"


def is_valid_obsidian_uri(uri: str) -> bool:
    """URI'nin güvenli bir `obsidian://open?` bağlantısı olduğunu doğrular."""
    if not isinstance(uri, str) or not uri.startswith("obsidian://open?"):
        return False
    if len(uri) > MAX_ARG_LEN or _CONTROL_CHAR_RE.search(uri):
        return False
    if any(ch.isspace() for ch in uri):
        return False
    return True


@dataclass(frozen=True)
class NotificationAction:
    """Omarchy bildirim aksiyonu (etiket + güvenli argv listesi)."""

    label: str
    argv: list[str]

    def to_omarchy_dict(self) -> dict[str, Any]:
        return {"label": self.label, "argv": list(self.argv)}


def _coerce_action(item: NotificationAction | dict[str, Any] | tuple[str, Sequence[str]]) -> NotificationAction:
    if isinstance(item, NotificationAction):
        return NotificationAction(label=item.label, argv=list(item.argv))
    if isinstance(item, dict):
        label = item.get("label")
        argv = item.get("argv")
        if not isinstance(label, str) or not isinstance(argv, (list, tuple)):
            raise ValueError("Aksiyon sözlüğü 'label' (str) ve 'argv' (list) içermelidir")
        return NotificationAction(label=label, argv=list(argv))
    if isinstance(item, tuple) and len(item) == 2:
        label, argv = item
        if not isinstance(label, str) or not isinstance(argv, (list, tuple)):
            raise ValueError("Aksiyon demeti (str, Sequence[str]) olmalıdır")
        return NotificationAction(label=label, argv=list(argv))
    raise TypeError(f"Desteklenmeyen aksiyon tipi: {type(item).__name__}")


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if not isinstance(argv, (list, tuple)) or len(argv) == 0:
        raise ValueError("Aksiyon argv listesi boş olamaz")
    if len(argv) > MAX_EXEC_ARGV_LEN:
        raise ValueError(f"Aksiyon argv en fazla {MAX_EXEC_ARGV_LEN} öğe olabilir")
    out: list[str] = []
    for idx, arg in enumerate(argv):
        if not isinstance(arg, str):
            raise ValueError(f"argv[{idx}] metin (str) olmalıdır")
        if len(arg) > MAX_ARG_LEN or "\x00" in arg:
            raise ValueError(f"argv[{idx}] geçersiz veya aşırı uzun")
        out.append(arg)
    if not out[0] or out[0].startswith("-"):
        raise ValueError("argv[0] boş olamaz ve '-' ile başlayamaz")
    return out


def is_destructive_action(action: NotificationAction) -> bool:
    """Aksiyonun iptal/silme/reddetme gibi yıkıcı bir işlem olup olmadığını denetler."""
    norm_label = _normalize_tr_lower(action.label)
    if any(token in norm_label for token in _DESTRUCTIVE_LABEL_TOKENS):
        return True
    if len(action.argv) >= 2:
        subcmd = _normalize_tr_lower(action.argv[1])
        if subcmd in _DESTRUCTIVE_ARGV_SUBCOMMANDS:
            return True
    return False


def validate_actions(
    actions: Sequence[NotificationAction | dict[str, Any] | tuple[str, Sequence[str]]],
    *,
    strict: bool = False,
) -> list[NotificationAction]:
    """Bildirim aksiyonlarını Omarchy kuralları ve güvenlik değişmeziyle doğrular.

    Güvenlik değişmezi:
    - En fazla 3 aksiyon kabul edilir.
    - Aksiyon #0 (kart tıklandığında tetiklenen birincil aksiyon) ASLA yıkıcı
      ("İptal", "cancel", vb.) olamaz. Güvenli bir aksiyon mevcutsa otomatik
      olarak ilk sıraya alınır (`strict=False`); güvenli alternatif yoksa
      veya `strict=True` ise `ValueError` fırlatılır.
    """
    if len(actions) > MAX_NOTIFICATION_ACTIONS:
        raise ValueError(
            f"Bir bildirimde en fazla {MAX_NOTIFICATION_ACTIONS} aksiyon olabilir (verilen: {len(actions)})"
        )

    normalized: list[NotificationAction] = []
    for raw in actions:
        act = _coerce_action(raw)
        label = act.label.strip()
        if not label or len(label) > MAX_ACTION_LABEL_LEN or _CONTROL_CHAR_RE.search(label):
            raise ValueError(f"Geçersiz aksiyon etiketi: {act.label!r}")
        argv = _validate_argv(act.argv)
        normalized.append(NotificationAction(label=label, argv=argv))

    if not normalized:
        return []

    if is_destructive_action(normalized[0]):
        if strict:
            raise ValueError(
                f"İlk bildirim aksiyonu yıkıcı olamaz: {normalized[0].label!r}"
            )
        safe_idx = next(
            (i for i, act in enumerate(normalized) if not is_destructive_action(act)),
            None,
        )
        if safe_idx is None:
            raise ValueError(
                "Bildirim yalnızca yıkıcı aksiyonlardan oluşamaz; ilk aksiyon güvenli olmalıdır"
            )
        safe_action = normalized.pop(safe_idx)
        normalized.insert(0, safe_action)

    return normalized


def _sanitize_glyph(glyph: str) -> str:
    if not glyph or len(glyph) > 16 or _CONTROL_CHAR_RE.search(glyph):
        return ""
    return glyph


def _is_omarchy_flag_collision(text: str) -> bool:
    if text in _OMARCHY_KNOWN_FLAGS:
        return True
    return any(
        text.startswith(prefix)
        for prefix in (
            "--glyph=",
            "--urgency=",
            "--app-name=",
            "--icon=",
            "--expire-time=",
            "--image=",
            "--replace-id=",
        )
    )


class OmarchyNotifier:
    """Omarchy masaüstü bildirim istemcisi (D-Bus birincil, CLI sıfır-bağımlılık yedek)."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg

    def send(
        self,
        summary: str,
        body: str = "",
        *,
        glyph: str = "",
        urgency: UrgencyLevel = "normal",
        replaces_id: int = 0,
        timeout_ms: int = -1,
        transient: bool = False,
        actions: Sequence[NotificationAction | dict[str, Any] | tuple[str, Sequence[str]]] = (),
        exec_argv: Sequence[str] | None = None,
    ) -> int:
        """Masaüstü bildirimi gönderir ve bildirim ID'sini döndürür."""
        if not self.cfg.desktop.enable_notifications:
            return 0

        validated_actions = validate_actions(actions)
        primary_exec_argv: list[str] | None = None
        if validated_actions:
            primary_exec_argv = list(validated_actions[0].argv)
        elif exec_argv is not None:
            primary_exec_argv = _validate_argv(exec_argv)

        clean_glyph = _sanitize_glyph(glyph)
        safe_replaces_id = max(0, int(replaces_id))
        safe_timeout = int(timeout_ms)

        from seslinot.platform.os_compat import current_platform

        plat = current_platform()
        if plat == "macos":
            return self._send_via_macos(summary=summary, body=body)
        if plat == "windows":
            return self._send_via_windows(summary=summary, body=body)

        dbus_id = self._send_via_dbus(
            summary=summary,
            body=body,
            glyph=clean_glyph,
            urgency=urgency,
            replaces_id=safe_replaces_id,
            timeout_ms=safe_timeout,
            transient=transient,
            actions=validated_actions,
            exec_argv=primary_exec_argv,
        )
        if dbus_id is not None:
            return dbus_id

        cli_id = self._send_via_omarchy_cli(
            summary=summary,
            body=body,
            glyph=clean_glyph,
            urgency=urgency,
            replaces_id=safe_replaces_id,
            timeout_ms=safe_timeout,
            transient=transient,
            actions=validated_actions,
            exec_argv=primary_exec_argv,
        )
        if cli_id is not None:
            return cli_id

        return self._send_via_notify_send(
            summary=summary,
            body=body,
            urgency=urgency,
            replaces_id=safe_replaces_id,
            timeout_ms=safe_timeout,
            transient=transient,
        )

    def _send_via_macos(self, *, summary: str, body: str) -> int:
        tn = shutil.which("terminal-notifier")
        if tn:
            cmd = [tn, "-title", self.cfg.desktop.app_name, "-subtitle", summary, "-message", body or summary]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0, check=False)
                return 1 if proc.returncode == 0 else 0
            except (OSError, subprocess.SubprocessError):
                return 0
        osa = shutil.which("osascript")
        if not osa:
            return 0
        esc_title = summary.replace("\\", "\\\\").replace('"', '\\"')
        esc_body = (body or summary).replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{esc_body}" with title "{self.cfg.desktop.app_name}" subtitle "{esc_title}"'
        try:
            proc = subprocess.run([osa, "-e", script], capture_output=True, text=True, timeout=5.0, check=False)
            return 1 if proc.returncode == 0 else 0
        except (OSError, subprocess.SubprocessError):
            return 0

    def _send_via_windows(self, *, summary: str, body: str) -> int:
        pwsh = shutil.which("powershell") or shutil.which("pwsh")
        if not pwsh:
            return 0
        from xml.sax.saxutils import escape as _xml_escape

        t_xml = _xml_escape(summary)
        b_xml = _xml_escape(body or "")
        app_xml = _xml_escape(self.cfg.desktop.app_name)
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
            f"$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$xml.LoadXml('<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{t_xml}</text><text id=\"2\">{b_xml}</text></binding></visual></toast>');"
            f"$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_xml}').Show($toast);"
        )
        try:
            proc = subprocess.run(
                [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            return 1 if proc.returncode == 0 else 0
        except (OSError, subprocess.SubprocessError):
            return 0

    def _build_dbus_hints(
        self,
        *,
        glyph: str,
        urgency: UrgencyLevel,
        transient: bool,
        actions: list[NotificationAction],
        exec_argv: list[str] | None,
    ) -> dict[str, Any]:
        hints: dict[str, Any] = {
            "urgency": _URGENCY_TO_BYTE[urgency],
            "transient": bool(transient),
        }
        if glyph:
            hints["omarchy-glyph"] = glyph
        if actions:
            hints["omarchy-actions"] = json.dumps(
                [a.to_omarchy_dict() for a in actions],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if exec_argv:
            hints["omarchy-exec-argv"] = json.dumps(
                list(exec_argv),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return hints

    def _send_via_dbus(
        self,
        *,
        summary: str,
        body: str,
        glyph: str,
        urgency: UrgencyLevel,
        replaces_id: int,
        timeout_ms: int,
        transient: bool,
        actions: list[NotificationAction],
        exec_argv: list[str] | None,
    ) -> int | None:
        try:
            import dbus  # type: ignore[import-not-found]
        except ImportError:
            return None

        try:
            bus = dbus.SessionBus()
            proxy = bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
            )
            iface = dbus.Interface(proxy, "org.freedesktop.Notifications")

            raw_hints = self._build_dbus_hints(
                glyph=glyph,
                urgency=urgency,
                transient=transient,
                actions=actions,
                exec_argv=exec_argv,
            )
            dbus_hints: dict[str, Any] = {}
            for key, value in raw_hints.items():
                if key == "urgency":
                    dbus_hints[key] = dbus.Byte(value)
                elif isinstance(value, bool):
                    dbus_hints[key] = dbus.Boolean(value)
                else:
                    dbus_hints[key] = dbus.String(value)

            returned = iface.Notify(
                dbus.String(self.cfg.desktop.app_name),
                dbus.UInt32(replaces_id),
                dbus.String(""),
                dbus.String(summary),
                dbus.String(body),
                dbus.Array([], signature="s"),
                dbus.Dictionary(dbus_hints, signature="sv"),
                dbus.Int32(timeout_ms),
            )
            return int(returned)
        except Exception as exc:
            logger.debug("D-Bus bildirimi başarısız, CLI yedeğine düşülüyor: %s", exc)
            return None

    def _build_omarchy_cli_cmd(
        self,
        binary: str,
        *,
        summary: str,
        body: str,
        glyph: str,
        urgency: UrgencyLevel,
        replaces_id: int,
        timeout_ms: int,
        transient: bool,
        actions: list[NotificationAction],
        exec_argv: list[str] | None,
    ) -> list[str]:
        cmd: list[str] = [
            binary,
            "--app-name",
            self.cfg.desktop.app_name,
            "-u",
            urgency,
            "-p",
        ]
        if glyph:
            cmd.extend(["-g", glyph])
        if replaces_id > 0:
            cmd.extend(["-r", str(replaces_id)])
        if timeout_ms >= 0:
            cmd.extend(["-t", str(timeout_ms)])
        if transient:
            cmd.append("--transient")

        cmd.append(summary)
        if body:
            safe_body = f" {body}" if _is_omarchy_flag_collision(body) else body
            cmd.append(safe_body)

        if actions:
            for action in actions:
                cmd.extend(["--action", action.label, *action.argv])
        elif exec_argv:
            cmd.extend(["--exec", *exec_argv])

        return cmd

    def _send_via_omarchy_cli(
        self,
        *,
        summary: str,
        body: str,
        glyph: str,
        urgency: UrgencyLevel,
        replaces_id: int,
        timeout_ms: int,
        transient: bool,
        actions: list[NotificationAction],
        exec_argv: list[str] | None,
    ) -> int | None:
        binary = shutil.which("omarchy-notification-send")
        if not binary:
            return None

        cmd = self._build_omarchy_cli_cmd(
            binary,
            summary=summary,
            body=body,
            glyph=glyph,
            urgency=urgency,
            replaces_id=replaces_id,
            timeout_ms=timeout_ms,
            transient=transient,
            actions=actions,
            exec_argv=exec_argv,
        )
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "omarchy-notification-send hata verdi (kod=%s): %s",
                    proc.returncode,
                    proc.stderr.strip(),
                )
                return None
            return _parse_notif_id(proc.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("omarchy-notification-send çalıştırılamadı: %s", exc)
            return None

    def _send_via_notify_send(
        self,
        *,
        summary: str,
        body: str,
        urgency: UrgencyLevel,
        replaces_id: int,
        timeout_ms: int,
        transient: bool,
    ) -> int:
        binary = shutil.which("notify-send")
        if not binary:
            logger.debug("Bildirim aracı bulunamadı (dbus / omarchy-notification-send / notify-send)")
            return 0

        cmd: list[str] = [
            binary,
            "--app-name",
            self.cfg.desktop.app_name,
            "-u",
            urgency,
            "-p",
        ]
        if replaces_id > 0:
            cmd.extend(["-r", str(replaces_id)])
        if timeout_ms >= 0:
            cmd.extend(["-t", str(timeout_ms)])
        if transient:
            cmd.extend(["-h", "boolean:transient:true"])
        cmd.append("--")
        cmd.append(summary)
        if body:
            cmd.append(body)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode != 0:
                return 0
            return _parse_notif_id(proc.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("notify-send çalıştırılamadı: %s", exc)
            return 0

    # --- Yüksek seviye SesliNot bildirim yardımcıları ---

    def _panel_open_argv(self) -> list[str]:
        return ["omarchy-shell", "-q", self.cfg.desktop.plugin_panel_target, "open"]

    def notify_note_saved(
        self,
        title: str,
        obsidian_uri: str = "",
        *,
        duration_secs: int = 0,
    ) -> int:
        body = f"{title} ({duration_secs} sn)" if duration_secs > 0 else title
        actions: list[NotificationAction] = []
        if is_valid_obsidian_uri(obsidian_uri):
            actions.append(
                NotificationAction(label="Obsidian'da Aç", argv=["xdg-open", obsidian_uri])
            )
        return self.send(
            "Sesli not kaydedildi",
            body,
            glyph="󰍬",
            urgency="low",
            timeout_ms=6000,
            transient=False,
            actions=actions,
        )

    def notify_research_started(self, job_id: str, topic: str) -> int:
        return self.send(
            "Derin araştırma başlatıldı",
            topic,
            glyph="󰖟",
            urgency="normal",
            timeout_ms=8000,
            transient=True,
            actions=[
                NotificationAction(label="Durumu Gör", argv=self._panel_open_argv()),
                NotificationAction(label="İptal Et", argv=["seslinot-ctl", "cancel", job_id]),
            ],
        )

    def notify_research_phase(
        self,
        notif_id: int,
        job_id: str,
        topic: str,
        phase_text: str,
    ) -> int:
        return self.send(
            f"Araştırma: {topic}",
            phase_text,
            glyph="󰖟",
            urgency="low",
            replaces_id=notif_id,
            timeout_ms=6000,
            transient=True,
            actions=[
                NotificationAction(label="Durumu Gör", argv=self._panel_open_argv()),
                NotificationAction(label="İptal Et", argv=["seslinot-ctl", "cancel", job_id]),
            ],
        )

    def notify_research_confirm(
        self,
        job_id: str,
        topic: str,
        confidence: float,
    ) -> int:
        pct = max(0, min(100, int(round(confidence * 100))))
        return self.send(
            "Derin araştırma başlatılsın mı?",
            f"{topic} (güven: %{pct})",
            glyph="󰋗",
            urgency="normal",
            timeout_ms=30000,
            transient=False,
            actions=[
                NotificationAction(
                    label="Araştırmayı Başlat",
                    argv=["seslinot-ctl", "confirm", job_id],
                ),
                NotificationAction(
                    label="Sadece Not Kalsın",
                    argv=["seslinot-ctl", "dismiss", job_id],
                ),
            ],
        )

    def notify_research_ready_for_approval(
        self,
        notif_id: int,
        job_id: str,
        topic: str,
        preview_path: str,
        source_count: int,
        confidence: str = "orta",
    ) -> int:
        """Araştırma bitince kasaya yazmadan önce kullanıcı onayına sunar.

        Aksiyon #0 her zaman güvenli okuma ('Önizle' veya 'Paneli Aç') olur.
        """
        meta = f"{source_count} doğrulanmış kaynak · güven: {confidence}"
        body = f"{topic}\n{meta}"
        actions: list[NotificationAction] = []
        if preview_path and Path(preview_path).is_file():
            actions.append(
                NotificationAction(label="Önizle", argv=["xdg-open", preview_path])
            )
        else:
            actions.append(
                NotificationAction(label="Paneli Aç", argv=self._panel_open_argv())
            )
        actions.append(
            NotificationAction(
                label="Kasaya Kaydet",
                argv=["seslinot-ctl", "approve-report", job_id],
            )
        )
        actions.append(
            NotificationAction(
                label="Reddet",
                argv=["seslinot-ctl", "reject-report", job_id],
            )
        )
        return self.send(
            "Araştırma hazır — Kaydedilsin mi?",
            body,
            glyph="󰄱",
            urgency="normal",
            replaces_id=notif_id,
            timeout_ms=30000,
            transient=False,
            actions=actions,
        )

    def notify_research_rejected(self, notif_id: int, topic: str) -> int:
        return self.send(
            "Rapor kasaya kaydedilmedi",
            topic,
            glyph="󰜺",
            urgency="low",
            replaces_id=notif_id,
            timeout_ms=5000,
            transient=True,
        )

    def notify_research_completed(
        self,
        notif_id: int,
        topic: str,
        obsidian_uri: str,
        source_count: int,
    ) -> int:
        body = (
            f"{topic} ({source_count} doğrulanmış kaynak)"
            if source_count > 0
            else topic
        )
        actions: list[NotificationAction] = []
        if is_valid_obsidian_uri(obsidian_uri):
            actions.append(
                NotificationAction(label="Obsidian'da Aç", argv=["xdg-open", obsidian_uri])
            )
        else:
            actions.append(
                NotificationAction(label="Paneli Aç", argv=self._panel_open_argv())
            )
        return self.send(
            "Araştırma kasaya kaydedildi",
            body,
            glyph="󰄬",
            urgency="normal",
            replaces_id=notif_id,
            timeout_ms=12000,
            transient=False,
            actions=actions,
        )

    def notify_research_cancelled(self, notif_id: int, topic: str) -> int:
        return self.send(
            "Araştırma iptal edildi",
            topic,
            glyph="󰜺",
            urgency="low",
            replaces_id=notif_id,
            timeout_ms=5000,
            transient=True,
        )

    def notify_research_failed(self, notif_id: int, topic: str, error: str) -> int:
        detail = f"{topic}: {error[:240]}" if error else topic
        return self.send(
            "Araştırma tamamlanamadı",
            detail,
            glyph="󰀪",
            urgency="critical",
            replaces_id=notif_id,
            timeout_ms=15000,
            transient=False,
            actions=[
                NotificationAction(label="Paneli Aç", argv=self._panel_open_argv()),
            ],
        )


def _parse_notif_id(stdout: str) -> int:
    text = stdout.strip()
    if not text:
        return 0
    token = text.split()[-1]
    try:
        return max(0, int(token))
    except ValueError:
        return 0


DesktopNotifier = OmarchyNotifier

