"""Windows, macOS ve X11 Linux için opsiyonel sistem tepsisi (tray) ve global kısayol uygulaması.

Opsiyonel bağımlılıklar: `pip install 'seslinot[desktop]'` (`pystray`, `pillow`, `pynput`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from seslinot.config import SesliNotConfig
from seslinot.desktop.daemon import SesliNotDaemon
from seslinot.models import JobStatus
from seslinot.pipeline import SesliNotPipeline
from seslinot.platform.os_compat import open_target

logger = logging.getLogger(__name__)


def _create_tray_image(recording: bool = False) -> Any:
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg = (224, 62, 62, 255) if recording else (58, 134, 255, 255)
    draw.ellipse((6, 6, size - 6, size - 6), fill=bg)
    draw.rounded_rectangle((25, 16, 39, 38), radius=6, fill=(255, 255, 255, 255))
    draw.line((32, 38, 32, 48), fill=(255, 255, 255, 255), width=4)
    draw.line((22, 48, 42, 48), fill=(255, 255, 255, 255), width=4)
    return img


class SesliNotTrayApp:
    """Çapraz platform sistem tepsisi + global kısayol + gömülü arka plan daemon yöneticisi."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg
        self.pipeline = SesliNotPipeline(cfg)
        self.daemon = SesliNotDaemon(
            cfg,
            db=self.pipeline.db,
            osd=self.pipeline.osd,
            notifier=self.pipeline.notifier,
            route_handler=self.pipeline.route_job,
            research_handler=self.pipeline.execute_research_job,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._daemon_thread: threading.Thread | None = None
        self._hotkey_listener: Any = None
        self._icon: Any = None
        self._recording = False
        self._lock = threading.Lock()

    def toggle_recording(self) -> None:
        with self._lock:
            try:
                res = asyncio.run(self.pipeline.toggle_recording())
                self._recording = res.get("action") == "started"
                if self._icon is not None:
                    self._icon.icon = _create_tray_image(self._recording)
                    self._icon.update_menu()
            except Exception as exc:
                logger.warning("Kayıt aç/kapa hatası: %s", exc)

    def _start_daemon_thread(self) -> None:
        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self.daemon.run_forever())
            finally:
                loop.close()

        self._daemon_thread = threading.Thread(target=_runner, name="seslinot-daemon", daemon=True)
        self._daemon_thread.start()

    def _start_hotkey_listener(self) -> None:
        try:
            from pynput import keyboard  # type: ignore[import-not-found]
        except ImportError:
            logger.info("pynput kurulu değil; global kısayol pasif (yalnız tepsi menüsü aktif).")
            return

        hotkey = self.cfg.desktop.hotkey_toggle or "<ctrl>+<alt>+n"
        try:
            listener = keyboard.GlobalHotKeys({hotkey: self.toggle_recording})
            listener.daemon = True
            listener.start()
            self._hotkey_listener = listener
            logger.info("Global kısayol dinleniyor: %s", hotkey)
        except Exception as exc:
            logger.warning("Global kısayol başlatılamadı (%s): %s", hotkey, exc)

    def _build_menu(self) -> Any:
        import pystray  # type: ignore[import-not-found]

        pending = self.pipeline.db.list_jobs([JobStatus.AWAITING_APPROVAL], limit=5)
        items: list[Any] = [
            pystray.MenuItem(
                "Kaydı Durdur ve Kaydet" if self._recording else "Sesli Not Kaydet",
                lambda _icon, _item: self.toggle_recording(),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
        ]
        if pending:
            approval_items = []
            for job in pending:
                jid = job.job_id
                title = (job.topic or jid)[:48]
                preview_path = f"{job.session_dir}/preview.md" if job.session_dir else ""
                approval_items.append(
                    pystray.MenuItem(
                        title,
                        pystray.Menu(
                            pystray.MenuItem(
                                "Önizle",
                                lambda _i, _it, p=preview_path: open_target(p),
                            ),
                            pystray.MenuItem(
                                "Kasaya Kaydet",
                                lambda _i, _it, j=jid: self.pipeline.approve_pending_report(j),
                            ),
                            pystray.MenuItem(
                                "Reddet",
                                lambda _i, _it, j=jid: self.pipeline.reject_pending_report(j),
                            ),
                        ),
                    )
                )
            items.append(
                pystray.MenuItem(
                    f"Kayıt Onayı Bekleyenler ({len(pending)})",
                    pystray.Menu(*approval_items),
                )
            )
            items.append(pystray.Menu.SEPARATOR)

        items.append(
            pystray.MenuItem(
                "Obsidian Kasasını Aç",
                lambda _icon, _item: open_target(str(self.cfg.vault.path)),
            )
        )
        items.append(pystray.MenuItem("Çıkış", self._on_quit))
        return pystray.Menu(*items)

    def _on_quit(self, icon: Any, _item: Any) -> None:
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.daemon.request_stop)
        icon.stop()

    def run(self) -> int:
        try:
            import pystray  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Sistem tepsisi için opsiyonel bağımlılıklar gerekli: "
                "uv tool install 'seslinot[desktop]' veya pip install 'seslinot[desktop]'"
            ) from exc

        self._start_daemon_thread()
        self._start_hotkey_listener()
        self._icon = pystray.Icon(
            "seslinot",
            _create_tray_image(False),
            title="SesliNot",
            menu=pystray.Menu(lambda: self._build_menu().items),
        )
        self._icon.run()
        return 0
