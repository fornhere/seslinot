"""Omarchy Shell OSD ve atomik durum dosyası (state.json) yöneticisi."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from seslinot.config import SesliNotConfig
from seslinot.models import DesktopState

logger = logging.getLogger(__name__)


class OmarchyOSD:
    """Omarchy Shell OSD bildirimlerini ve bar eklentisi durum dosyasını yönetir."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg

    def _spawn_omarchy_shell(self, args: list[str]) -> bool:
        binary = shutil.which("omarchy-shell")
        if not binary:
            logger.debug("omarchy-shell bulunamadı; IPC atlandı: %s", args)
            return False

        cmd = [binary, "-q", *args]
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError as exc:
            logger.warning("omarchy-shell başlatılamadı (%s): %s", args, exc)
            return False

    def show(
        self,
        message: str,
        *,
        icon: str = "microphone",
        duration_ms: int = 0,
    ) -> bool:
        """Omarchy OSD kartını bloklamadan gösterir.

        duration_ms == 0 olduğunda OSD manuel kapatılana kadar açık kalır.
        """
        if not self.cfg.desktop.enable_osd:
            return False

        payload = {
            "icon": icon,
            "message": message,
            "duration": max(0, int(duration_ms)),
        }
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self._spawn_omarchy_shell(["osd", "show", payload_json])

    def close(self) -> bool:
        """Açık Omarchy OSD kartını bloklamadan kapatır."""
        if not self.cfg.desktop.enable_osd:
            return False
        return self._spawn_omarchy_shell(["osd", "close"])

    def refresh_bar(self) -> bool:
        """Omarchy bar eklentisinin state.json'ı yeniden okumasını tetikler."""
        if not self.cfg.desktop.enable_bar_refresh:
            return False
        target = self.cfg.desktop.plugin_ipc_target.strip()
        if not target:
            return False
        return self._spawn_omarchy_shell([target, "refresh"])

    def write_desktop_state(self, state: DesktopState) -> Path:
        """DesktopState'i atomik (tmp -> fsync -> os.replace) olarak state.json'a yazar."""
        state_path = self.cfg.paths.state_json_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2) + "\n"

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=state_path.parent,
                prefix=f".{state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(tmp_path, state_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        if self.cfg.desktop.enable_bar_refresh:
            self.refresh_bar()

        return state_path
