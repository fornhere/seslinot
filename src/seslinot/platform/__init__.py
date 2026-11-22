"""Çapraz platform (Linux, macOS, Windows) işletim sistemi soyutlama katmanı."""

from seslinot.platform.os_compat import (
    ObsidianVaultInfo,
    current_platform,
    default_config_dir,
    default_data_dir,
    default_runtime_dir,
    default_state_dir,
    default_user_name,
    detect_obsidian_vaults,
    exclusive_file_lock,
    is_hafiza_os_vault,
    open_command_argv,
    open_target,
    popen_isolated_kwargs,
    terminate_process_tree_async,
)

__all__ = [
    "ObsidianVaultInfo",
    "current_platform",
    "default_config_dir",
    "default_data_dir",
    "default_runtime_dir",
    "default_state_dir",
    "default_user_name",
    "detect_obsidian_vaults",
    "exclusive_file_lock",
    "is_hafiza_os_vault",
    "open_command_argv",
    "open_target",
    "popen_isolated_kwargs",
    "terminate_process_tree_async",
]
