"""Çapraz platform (Linux/macOS/Windows) yol, dosya kilidi, süreç ve Obsidian keşif testleri."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from seslinot.platform.os_compat import (
    current_platform,
    default_config_dir,
    default_data_dir,
    default_runtime_dir,
    default_state_dir,
    detect_obsidian_vaults,
    exclusive_file_lock,
    is_hafiza_os_vault,
    open_command_argv,
    popen_isolated_kwargs,
    terminate_process_tree_async,
)


def test_platform_normalization_and_directories(tmp_path: Path) -> None:
    assert current_platform("linux") == "linux"
    assert current_platform("darwin") == "macos"
    assert current_platform("win32") == "windows"

    fake_home = tmp_path / "home"
    # Linux XDG
    linux_env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg_cfg"),
        "XDG_DATA_HOME": str(tmp_path / "xdg_data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg_state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg_run"),
    }
    assert default_config_dir(sys_platform="linux", env=linux_env, home=fake_home) == tmp_path / "xdg_cfg" / "seslinot"
    assert default_data_dir(sys_platform="linux", env=linux_env, home=fake_home) == tmp_path / "xdg_data" / "seslinot"
    assert default_state_dir(sys_platform="linux", env=linux_env, home=fake_home) == tmp_path / "xdg_state" / "seslinot"
    assert default_runtime_dir(sys_platform="linux", env=linux_env) == tmp_path / "xdg_run" / "seslinot"

    # macOS
    assert default_config_dir(sys_platform="darwin", env={}, home=fake_home) == fake_home / "Library/Application Support/SesliNot"
    assert default_data_dir(sys_platform="darwin", env={}, home=fake_home) == fake_home / "Library/Application Support/SesliNot/data"
    assert default_state_dir(sys_platform="darwin", env={}, home=fake_home) == fake_home / "Library/Caches/SesliNot/state"

    # Windows
    win_env = {
        "APPDATA": str(tmp_path / "AppData/Roaming"),
        "LOCALAPPDATA": str(tmp_path / "AppData/Local"),
    }
    assert default_config_dir(sys_platform="win32", env=win_env, home=fake_home) == tmp_path / "AppData/Roaming/SesliNot"
    assert default_data_dir(sys_platform="win32", env=win_env, home=fake_home) == tmp_path / "AppData/Local/SesliNot/data"
    assert default_state_dir(sys_platform="win32", env=win_env, home=fake_home) == tmp_path / "AppData/Local/SesliNot/state"
    assert default_runtime_dir(sys_platform="win32", env=win_env) == tmp_path / "AppData/Local/SesliNot/runtime"


def test_exclusive_file_lock_preserves_file_and_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "vault.lock"
    with exclusive_file_lock(lock_path, timeout=2.0):
        assert lock_path.is_file()
    # Kilit dosyası asla silinmez ve hemen yeniden alınabilir
    assert lock_path.is_file()
    with exclusive_file_lock(lock_path, timeout=2.0):
        pass


def test_popen_isolated_kwargs_and_open_command_argv() -> None:
    assert popen_isolated_kwargs(sys_platform="linux") == {"start_new_session": True}
    assert popen_isolated_kwargs(sys_platform="darwin") == {"start_new_session": True}
    win_kw = popen_isolated_kwargs(sys_platform="win32")
    assert "creationflags" in win_kw
    assert "start_new_session" not in win_kw

    assert open_command_argv("obsidian://open?vault=V", sys_platform="linux") == ["xdg-open", "obsidian://open?vault=V"]
    assert open_command_argv("obsidian://open?vault=V", sys_platform="darwin") == ["open", "obsidian://open?vault=V"]
    assert open_command_argv("obsidian://open?vault=V", sys_platform="win32") == ["cmd", "/c", "start", "", "obsidian://open?vault=V"]


def test_detect_obsidian_vaults_and_hafiza_os_detection(tmp_path: Path) -> None:
    std_vault = tmp_path / "Notes"
    std_vault.mkdir()
    hafiza_vault = tmp_path / "MyMemory"
    (hafiza_vault / "araclar").mkdir(parents=True)
    (hafiza_vault / "araclar" / "hafiza.py").write_text("# stub\n", encoding="utf-8")

    assert is_hafiza_os_vault(std_vault) is False
    assert is_hafiza_os_vault(hafiza_vault) is True

    obsidian_json = tmp_path / "obsidian.json"
    obsidian_json.write_text(
        json.dumps(
            {
                "vaults": {
                    "a1": {"path": str(std_vault), "open": False},
                    "b2": {"path": str(hafiza_vault), "open": True},
                    "missing": {"path": str(tmp_path / "DoesNotExist"), "open": True},
                }
            }
        ),
        encoding="utf-8",
    )

    vaults = detect_obsidian_vaults(obsidian_json)
    assert len(vaults) == 2
    assert vaults[0].name == "MyMemory"
    assert vaults[0].is_open is True
    assert vaults[0].is_hafiza_os is True
    assert vaults[1].name == "Notes"
    assert vaults[1].is_hafiza_os is False


@pytest.mark.asyncio
async def test_terminate_process_tree_async_stops_child() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **popen_isolated_kwargs(),
    )
    await terminate_process_tree_async(proc)
    assert proc.returncode is not None
