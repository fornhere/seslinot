"""Obsidian / Hafıza OS kasa yazıcı, kasa-dışı FTS5 dizin ve köprü katmanı."""

from __future__ import annotations

from seslinot.vault.hafiza_bridge import HafizaBridge
from seslinot.vault.index import EXCLUDED_DIRS, VaultIndex, fold_turkish
from seslinot.vault.writer import (
    FORBIDDEN_WRITE_DIRS,
    SECRET_RULES,
    VaultWriter,
    atomic_write_text,
    contains_secret,
    is_referenced_in_hafiza_memory,
    safe_vault_path,
    scan_and_mask_secrets,
    turkish_lower,
    turkish_slug,
)

__all__ = [
    "EXCLUDED_DIRS",
    "FORBIDDEN_WRITE_DIRS",
    "HafizaBridge",
    "SECRET_RULES",
    "VaultIndex",
    "VaultWriter",
    "atomic_write_text",
    "contains_secret",
    "fold_turkish",
    "is_referenced_in_hafiza_memory",
    "safe_vault_path",
    "scan_and_mask_secrets",
    "turkish_lower",
    "turkish_slug",
]
