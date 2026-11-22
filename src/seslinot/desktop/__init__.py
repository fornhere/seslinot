"""SesliNot Omarchy masaüstü entegrasyon paketi (OSD, bildirimler, daemon)."""

from __future__ import annotations

from seslinot.desktop.daemon import (
    DaemonTickSummary,
    NetworkMonitor,
    ResearchExecutionResult,
    SesliNotDaemon,
    main,
)
from seslinot.desktop.notify import (
    NotificationAction,
    OmarchyNotifier,
    is_destructive_action,
    is_valid_obsidian_uri,
    make_obsidian_uri,
    validate_actions,
)
from seslinot.desktop.osd import OmarchyOSD

__all__ = [
    "DaemonTickSummary",
    "NetworkMonitor",
    "NotificationAction",
    "OmarchyNotifier",
    "OmarchyOSD",
    "ResearchExecutionResult",
    "SesliNotDaemon",
    "is_destructive_action",
    "is_valid_obsidian_uri",
    "main",
    "make_obsidian_uri",
    "validate_actions",
]
