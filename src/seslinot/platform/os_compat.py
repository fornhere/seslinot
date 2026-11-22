"""Linux, macOS ve Windows uyumlu yol, dosya kilidi, süreç ağacı ve Obsidian keşfi.

Sıfır ek bağımlılıkla yalnızca Python standart kütüphanesi kullanır.
"""

from __future__ import annotations

import asyncio
import errno
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

PlatformName = Literal["linux", "macos", "windows"]


def current_platform(sys_platform: str | None = None) -> PlatformName:
    """Aktif işletim sistemini normalize eder: 'linux', 'macos' veya 'windows'."""
    plat = (sys_platform or sys.platform).lower()
    if plat.startswith("win") or plat in {"cygwin", "msys"}:
        return "windows"
    if plat == "darwin":
        return "macos"
    return "linux"


def default_user_name() -> str:
    """Taşınabilir geçerli OS kullanıcı adını döndürür."""
    try:
        user = getpass.getuser().strip()
        if user:
            return user
    except Exception:
        pass
    return (os.environ.get("USER") or os.environ.get("USERNAME") or "user").strip() or "user"


def default_config_dir(
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Platform kurallarına uygun yapılandırma dizini (XDG / AppData / Application Support)."""
    plat = current_platform(sys_platform)
    env_map = env if env is not None else os.environ
    home_dir = home if home is not None else Path.home()

    if plat == "windows":
        base = env_map.get("APPDATA") or str(home_dir / "AppData" / "Roaming")
        return Path(base) / "SesliNot"
    if plat == "macos":
        return home_dir / "Library" / "Application Support" / "SesliNot"
    xdg = env_map.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home_dir / ".config") / "seslinot"


def default_data_dir(
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Platform kurallarına uygun kalıcı veri dizini (DB, ses arşivi, FTS5)."""
    plat = current_platform(sys_platform)
    env_map = env if env is not None else os.environ
    home_dir = home if home is not None else Path.home()

    if plat == "windows":
        base = env_map.get("LOCALAPPDATA") or env_map.get("APPDATA") or str(home_dir / "AppData" / "Local")
        return Path(base) / "SesliNot" / "data"
    if plat == "macos":
        return home_dir / "Library" / "Application Support" / "SesliNot" / "data"
    xdg = env_map.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home_dir / ".local" / "share") / "seslinot"


def default_state_dir(
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Platform kurallarına uygun durum ve kilit dosyası dizini."""
    plat = current_platform(sys_platform)
    env_map = env if env is not None else os.environ
    home_dir = home if home is not None else Path.home()

    if plat == "windows":
        base = env_map.get("LOCALAPPDATA") or env_map.get("APPDATA") or str(home_dir / "AppData" / "Local")
        return Path(base) / "SesliNot" / "state"
    if plat == "macos":
        return home_dir / "Library" / "Caches" / "SesliNot" / "state"
    xdg = env_map.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else home_dir / ".local" / "state") / "seslinot"


def default_runtime_dir(
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Platform kurallarına uygun geçici çalışma zamanı dizini (state.json, capture.json)."""
    plat = current_platform(sys_platform)
    env_map = env if env is not None else os.environ

    if plat == "linux":
        xdg = env_map.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / "seslinot"
        uid = os.getuid() if hasattr(os, "getuid") else default_user_name()
        return Path(tempfile.gettempdir()) / f"seslinot-{uid}"
    if plat == "macos":
        return Path(tempfile.gettempdir()) / f"seslinot-{default_user_name()}"
    # Windows
    local = env_map.get("LOCALAPPDATA")
    if local:
        return Path(local) / "SesliNot" / "runtime"
    return Path(tempfile.gettempdir()) / f"seslinot-{default_user_name()}"


@contextmanager
def exclusive_file_lock(lock_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Linux/macOS (`fcntl.flock`) ve Windows (`msvcrt.locking`) uyumlu kalıcı dosya kilidi.

    Kilit dosyası asla silinmez (tüm bekleyenler aynı inode'u paylaşır) ve dosya
    içeriği değiştirilmez.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(str(lock_path), flags, 0o600)
    acquired = False
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

    try:
        if os.name == "nt":
            import msvcrt  # type: ignore[import-not-found]

            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError(f"Kilit zaman aşımı: {lock_path}") from None
                        time.sleep(0.01 if deadline is not None else 0.05)
                        continue
                    raise
        else:
            import fcntl

            while True:
                try:
                    lock_flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if deadline is not None else 0)
                    fcntl.flock(fd, lock_flags)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    if deadline is not None and exc.errno in (errno.EACCES, errno.EAGAIN):
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Kilit zaman aşımı: {lock_path}") from None
                        time.sleep(0.01)
                        continue
                    raise

        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt  # type: ignore[import-not-found]

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def popen_isolated_kwargs(*, sys_platform: str | None = None) -> dict[str, Any]:
    """Alt süreçleri ayrı süreç grubunda başlatmak için platforma uygun Popen/asyncio kwargs."""
    if current_platform(sys_platform) == "windows":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags | no_window}
    return {"start_new_session": True}


async def terminate_process_tree_async(proc: asyncio.subprocess.Process) -> None:
    """POSIX süreç grubunu (`killpg`) veya Windows süreç ağacını (`taskkill /T /F`) temiz kapatır."""
    if proc.returncode is not None:
        return

    if current_platform() == "windows":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                tk = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(tk.wait(), timeout=3.0)
            except (OSError, TimeoutError):
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.5)
        except TimeoutError:
            pass
        return

    # POSIX: süreç grubuna önce SIGTERM, ardından SIGKILL
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=2.5)
    except TimeoutError:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            pass


def open_command_argv(target: str, *, sys_platform: str | None = None) -> list[str]:
    """Dosya veya URI açmak için işletim sistemine uygun argv döndürür."""
    plat = current_platform(sys_platform)
    if plat == "macos":
        return ["open", target]
    if plat == "windows":
        return ["cmd", "/c", "start", "", target]
    return ["xdg-open", target]


def open_target(target: str) -> bool:
    """Verilen dosya yolunu veya `obsidian://` URI'sini varsayılan uygulamada açar."""
    if not target:
        return False
    if current_platform() == "windows" and hasattr(os, "startfile"):
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    cmd = open_command_argv(target)
    if shutil.which(cmd[0]) is None:
        return False
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_isolated_kwargs(),
        )
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class ObsidianVaultInfo:
    """Obsidian `obsidian.json` içinden keşfedilen kasa bilgisi."""

    name: str
    path: Path
    is_open: bool = False
    is_hafiza_os: bool = False


def obsidian_config_candidates(
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Linux, macOS ve Windows'ta Obsidian'ın `obsidian.json` olası yollarını döndürür."""
    plat = current_platform(sys_platform)
    env_map = env if env is not None else os.environ
    home_dir = home if home is not None else Path.home()

    if plat == "windows":
        appdata = Path(env_map.get("APPDATA") or str(home_dir / "AppData" / "Roaming"))
        return [appdata / "obsidian" / "obsidian.json"]
    if plat == "macos":
        return [home_dir / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    xdg = Path(env_map.get("XDG_CONFIG_HOME") or str(home_dir / ".config"))
    return [
        xdg / "obsidian" / "obsidian.json",
        home_dir / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
        home_dir / "snap" / "obsidian" / "current" / ".config" / "obsidian" / "obsidian.json",
    ]


def is_hafiza_os_vault(vault_path: Path) -> bool:
    """Kasanın Hafıza OS yapısına sahip olup olmadığını salt-okunur olarak tespit eder."""
    try:
        root = Path(vault_path).expanduser().resolve()
    except OSError:
        return False
    if not root.is_dir():
        return False
    has_tool = (root / "araclar" / "hafiza.py").is_file()
    has_catalog = (root / "zihin" / "hafıza-kataloğu.jsonl").is_file()
    has_concepts = (root / "komuta" / "kavramlar.json").is_file()
    return has_tool or (has_catalog and has_concepts)


def detect_obsidian_vaults(
    obsidian_json: Path | None = None,
    *,
    sys_platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[ObsidianVaultInfo]:
    """Kurulu Obsidian kasalarını otomatik keşfeder (açık olan kasalar önce gelir)."""
    candidates = [obsidian_json] if obsidian_json is not None else obsidian_config_candidates(
        sys_platform=sys_platform, env=env, home=home
    )
    found: list[ObsidianVaultInfo] = []
    seen_paths: set[str] = set()

    for cfg_file in candidates:
        if not cfg_file.is_file():
            continue
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vaults = raw.get("vaults")
        if not isinstance(vaults, dict):
            continue
        for entry in vaults.values():
            if not isinstance(entry, dict):
                continue
            raw_path = entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            p = Path(raw_path).expanduser()
            key = str(p)
            if key in seen_paths or not p.is_dir():
                continue
            seen_paths.add(key)
            found.append(
                ObsidianVaultInfo(
                    name=p.name,
                    path=p,
                    is_open=bool(entry.get("open")),
                    is_hafiza_os=is_hafiza_os_vault(p),
                )
            )

    found.sort(key=lambda v: (not v.is_open, not v.is_hafiza_os, v.name.casefold()))
    return found
