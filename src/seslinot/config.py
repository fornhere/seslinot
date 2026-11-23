"""SesliNot yapılandırma şeması ve yükleyicisi."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from seslinot.platform.os_compat import (
    current_platform,
    default_config_dir,
    default_data_dir,
    default_runtime_dir,
    default_state_dir,
)

DEFAULT_INITIAL_PROMPT = (
    "Claude Code, Codex, Git, branch, commit, push, prompt, prompt injection, "
    "agent, subagent, model, benchmark, deploy, frontend, backend, bug, fix, API, "
    "GPU, open source, fine-tune, render, workflow, Obsidian, Markdown, DeepSeek, "
    "Claude, Sonnet, Gemini, Wayland, PipeWire, Windows, macOS, Linux, terminal."
)


def _read_voxtype_initial_prompt() -> str | None:
    if current_platform() != "linux":
        return None
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
    path: Path = Field(default_factory=lambda: Path.home() / "Obsidian" / "SesliNot")
    vault_name: str = ""
    voice_dir: str = "sesli-notlar"
    research_dir: str = "araştırmalar"
    hafiza_tools_dir: str = "araclar"
    hafiza_os_mode: Literal["auto", "enabled", "disabled"] = "auto"
    personal_terms: list[str] = Field(default_factory=list)
    auto_update_maps: bool = True

    def model_post_init(self, __context: object) -> None:
        if not self.vault_name:
            self.vault_name = self.path.name or "SesliNot"


class PathsConfig(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir)
    state_dir: Path = Field(default_factory=default_state_dir)
    runtime_dir: Path = Field(default_factory=default_runtime_dir)

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
    capture_backend: Literal["auto", "pipewire", "sounddevice"] = "auto"
    stt_provider: Literal["local", "groq", "openai", "custom"] = "local"
    stt_endpoint: str = "http://127.0.0.1:8178/v1/audio/transcriptions"
    stt_model: str = "whisper-1"
    stt_api_key_env: str = ""
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
    # Direct API modu (CLI aboneliği olmayan Windows/macOS/Linux kullanıcıları için)
    api_provider: Literal["openai", "anthropic"] = "openai"
    api_base_url: str = ""
    api_key_env: str = ""
    api_model: str = "gpt-4o-mini"
    search_provider: Literal["auto", "tavily", "brave", "ddg"] = "auto"
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
    hotkey_toggle: str = "<ctrl>+<alt>+n"


class SesliNotConfig(BaseModel):
    vault: VaultConfig = Field(default_factory=VaultConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)


ALLOWED_SECRET_KEYS: tuple[str, ...] = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
)


def default_config_path() -> Path:
    env_path = os.environ.get("SESLINOT_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return default_config_dir() / "config.toml"


def default_secrets_path(cfg_path: Path | None = None) -> Path:
    base = (cfg_path or default_config_path()).parent
    return base / "secrets.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            val = v.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
                val = val[1:-1]
            if key in ALLOWED_SECRET_KEYS and val:
                values[key] = val
    except OSError:
        return {}
    return values


def load_secrets_env(cfg_path: Path | None = None) -> dict[str, str]:
    """Kullanıcı yapılandırma dizinindeki `secrets.env` dosyasını ortam değişkenlerine yükler."""
    sec_path = default_secrets_path(cfg_path)
    loaded = _parse_env_file(sec_path)
    for k, v in loaded.items():
        os.environ.setdefault(k, v)
    return loaded


def save_secrets_env(updates: dict[str, str | None], cfg_path: Path | None = None) -> dict[str, bool]:
    """API anahtarlarını kasa dışında, 0600 izinli `secrets.env` dosyasında güvenle saklar."""
    sec_path = default_secrets_path(cfg_path)
    existing = _parse_env_file(sec_path)
    for key, val in updates.items():
        if key not in ALLOWED_SECRET_KEYS:
            continue
        if val is None:
            continue
        clean = val.strip()
        if clean:
            existing[key] = clean
            os.environ[key] = clean
        else:
            existing.pop(key, None)
            os.environ.pop(key, None)

    sec_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# SesliNot yerel API anahtarları (kasa dışındadır, asla paylaşılmaz)"]
    for key in ALLOWED_SECRET_KEYS:
        if key in existing:
            escaped = existing[key].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
    sec_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(sec_path, 0o600)
        except OSError:
            pass
    return {key: bool(os.environ.get(key, "").strip()) for key in ALLOWED_SECRET_KEYS}


def write_config_toml(cfg: SesliNotConfig, cfg_path: Path) -> Path:
    """`SesliNotConfig` nesnesini temiz, okunabilir ve çapraz-platform geçerli TOML olarak yazar."""
    import json as _json

    def _q(val: str) -> str:
        return _json.dumps(val, ensure_ascii=False)

    def _b(val: bool) -> str:
        return "true" if val else "false"

    def _list(items: list[str]) -> str:
        return "[" + ", ".join(_q(x) for x in items) + "]"

    lines = [
        "# SesliNot yapılandırma dosyası",
        "",
        "[vault]",
        f"path = {_q(Path(cfg.vault.path).expanduser().as_posix())}",
        f"vault_name = {_q(cfg.vault.vault_name)}",
        f"voice_dir = {_q(cfg.vault.voice_dir)}",
        f"research_dir = {_q(cfg.vault.research_dir)}",
        f"hafiza_os_mode = {_q(cfg.vault.hafiza_os_mode)}",
        f"personal_terms = {_list(cfg.vault.personal_terms)}",
        f"auto_update_maps = {_b(cfg.vault.auto_update_maps)}",
        "",
        "[audio]",
        f"capture_backend = {_q(cfg.audio.capture_backend)}",
        f"stt_provider = {_q(cfg.audio.stt_provider)}",
        f"stt_endpoint = {_q(cfg.audio.stt_endpoint)}",
        f"stt_model = {_q(cfg.audio.stt_model)}",
        f"stt_api_key_env = {_q(cfg.audio.stt_api_key_env)}",
        f"stt_fallback_voxtype = {_b(cfg.audio.stt_fallback_voxtype)}",
        f"language = {_q(cfg.audio.language)}",
        f"duck_enabled = {_b(cfg.audio.duck_enabled)}",
        f"cues_enabled = {_b(cfg.audio.cues_enabled)}",
        f"retention = {_q(cfg.audio.retention)}",
        "",
        "[router]",
        f"backends = {_list(cfg.router.backends)}",
        f"auto_start_threshold = {cfg.router.auto_start_threshold}",
        f"confirm_threshold = {cfg.router.confirm_threshold}",
        f"claude_model = {_q(cfg.router.claude_model)}",
        f"agy_model = {_q(cfg.router.agy_model)}",
        f"codex_model = {_q(cfg.router.codex_model)}",
        "",
        "[research]",
        f"lead_backend = {_q(cfg.research.lead_backend)}",
        f"lead_model = {_q(cfg.research.lead_model)}",
        f"worker_backend = {_q(cfg.research.worker_backend)}",
        f"worker_model = {_q(cfg.research.worker_model)}",
        f"worker_fallback_backend = {_q(cfg.research.worker_fallback_backend)}",
        f"worker_fallback_model = {_q(cfg.research.worker_fallback_model)}",
        f"synth_backend = {_q(cfg.research.synth_backend)}",
        f"synth_model = {_q(cfg.research.synth_model)}",
        f"audit_backend = {_q(cfg.research.audit_backend)}",
        f"audit_model = {_q(cfg.research.audit_model)}",
        f"audit_fallback_backend = {_q(cfg.research.audit_fallback_backend)}",
        f"audit_fallback_model = {_q(cfg.research.audit_fallback_model)}",
        f"api_provider = {_q(cfg.research.api_provider)}",
        f"api_base_url = {_q(cfg.research.api_base_url)}",
        f"api_key_env = {_q(cfg.research.api_key_env)}",
        f"api_model = {_q(cfg.research.api_model)}",
        f"search_provider = {_q(cfg.research.search_provider)}",
        f"max_subagents = {cfg.research.max_subagents}",
        f"use_systemd_run = {_b(cfg.research.use_systemd_run)}",
        f"require_save_approval = {_b(cfg.research.require_save_approval)}",
        "",
        "[desktop]",
        f"enable_osd = {_b(cfg.desktop.enable_osd)}",
        f"enable_notifications = {_b(cfg.desktop.enable_notifications)}",
        f"enable_bar_refresh = {_b(cfg.desktop.enable_bar_refresh)}",
        f"hotkey_toggle = {_q(cfg.desktop.hotkey_toggle)}",
        "",
    ]
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    return cfg_path


def load_config(path: Path | None = None) -> SesliNotConfig:
    cfg_path = path or default_config_path()
    load_secrets_env(cfg_path)
    data: dict = {}
    if cfg_path.is_file():
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = SesliNotConfig.model_validate(data)
    vault_override = os.environ.get("SESLINOT_VAULT")
    if vault_override:
        cfg.vault.path = Path(vault_override).expanduser()
        cfg.vault.vault_name = cfg.vault.path.name
    return cfg
