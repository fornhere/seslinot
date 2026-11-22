"""SesliNot yapılandırma şeması ve yükleyicisi."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_INITIAL_PROMPT = (
    "Claude Code'da yeni bir branch açtım, commit attım ve push ettim. "
    "Prompt, prompt injection, agent, subagent, model, benchmark, deploy, "
    "frontend, backend, bug, fix, API, GPU, open source, fine-tune, render, "
    "workflow, Obsidian, Markdown, DeepSeek, Kimi, Codex, Claude, Opus, Sonnet, "
    "Gemini, Hyprland, Omarchy, PipeWire, Wayland, terminal, YouTube, thumbnail."
)


def _default_runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "seslinot"
    return Path(f"/tmp/seslinot-{os.getuid()}")


def _read_voxtype_initial_prompt() -> str | None:
    cfg = Path.home() / ".config/voxtype/config.toml"
    if not cfg.is_file():
        return None
    try:
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
        prompt = raw.get("whisper", {}).get("initial_prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt.strip()
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return None


class VaultConfig(BaseModel):
    path: Path = Field(default_factory=lambda: Path.home() / "Hafıza")
    vault_name: str = "Hafıza"
    voice_dir: str = "sesli-notlar"
    research_dir: str = "araştırmalar"
    hafiza_tools_dir: str = "araclar"
    auto_update_maps: bool = True


class PathsConfig(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".local/share/seslinot")
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".local/state/seslinot")
    runtime_dir: Path = Field(default_factory=_default_runtime_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "seslinot.db"

    @property
    def fts_db_path(self) -> Path:
        return self.data_dir / "vault_fts.sqlite"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def spool_dir(self) -> Path:
        return self.data_dir / "spool"

    @property
    def sounds_dir(self) -> Path:
        return self.data_dir / "sounds"

    @property
    def vault_lock_path(self) -> Path:
        return self.state_dir / "vault.lock"

    @property
    def state_json_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def capture_state_path(self) -> Path:
        return self.runtime_dir / "capture.json"

    @property
    def duck_state_path(self) -> Path:
        return self.runtime_dir / "duck_state.json"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.state_dir,
            self.runtime_dir,
            self.sessions_dir,
            self.audio_dir,
            self.spool_dir,
            self.sounds_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    stt_endpoint: str = "http://127.0.0.1:8178/v1/audio/transcriptions"
    stt_model: str = "whisper-1"
    stt_timeout_secs: float = 120.0
    stt_fallback_voxtype: bool = True
    language: str = "tr"
    initial_prompt: str = Field(
        default_factory=lambda: _read_voxtype_initial_prompt() or DEFAULT_INITIAL_PROMPT
    )
    vad_target_secs: float = 22.0
    vad_max_secs: float = 28.0
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 600
    vad_pad_ms: int = 200
    duck_enabled: bool = True
    duck_ratio: float = 0.30
    cues_enabled: bool = True
    cue_volume: float = 0.55
    retention: str = "opus_30d"  # opus | opus_30d | delete_after_verify


class RouterConfig(BaseModel):
    auto_start_threshold: float = 0.80
    confirm_threshold: float = 0.55
    backends: list[str] = Field(default_factory=lambda: ["claude", "agy", "codex"])
    agy_model: str = "gemini-3.8-flash-low"
    claude_model: str = "haiku"
    codex_model: str = "gpt-6-luna"
    timeout_secs: float = 25.0


class ResearchConfig(BaseModel):
    lead_backend: str = "claude"
    lead_model: str = "sonnet"
    worker_backend: str = "codex"
    worker_model: str = "gpt-6-luna"
    worker_reasoning_effort: str = "medium"
    worker_fallback_backend: str = "claude"
    worker_fallback_model: str = "sonnet"
    synth_backend: str = "claude"
    synth_model: str = "sonnet"
    audit_backend: str = "codex"
    audit_model: str = "gpt-6-luna"
    audit_fallback_backend: str = "claude"
    audit_fallback_model: str = "sonnet"
    max_subagents: int = 4
    max_concurrent_jobs: int = 2
    worker_timeout_secs: int = 420
    use_systemd_run: bool = True
    max_budget_usd: float = 1.50
    repair_max_rounds: int = 1
    default_freshness_months: int = 3
    fetch_concurrency: int = 8
    fetch_timeout_secs: float = 10.0
    require_save_approval: bool = True


class DesktopConfig(BaseModel):
    enable_osd: bool = True
    enable_notifications: bool = True
    enable_bar_refresh: bool = True
    plugin_ipc_target: str = "forn.seslinot.state"
    plugin_panel_target: str = "forn.seslinot"
    app_name: str = "SesliNot"
    poll_interval_secs: float = 2.0


class SesliNotConfig(BaseModel):
    vault: VaultConfig = Field(default_factory=VaultConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)


def default_config_path() -> Path:
    env_path = os.environ.get("SESLINOT_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config/seslinot/config.toml"


def load_config(path: Path | None = None) -> SesliNotConfig:
    cfg_path = path or default_config_path()
    data: dict = {}
    if cfg_path.is_file():
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = SesliNotConfig.model_validate(data)
    vault_override = os.environ.get("SESLINOT_VAULT")
    if vault_override:
        cfg.vault.path = Path(vault_override).expanduser()
        cfg.vault.vault_name = cfg.vault.path.name
    return cfg
