/**
 * SesliNot lokal HTTP+SSE istemcisi ve Tauri v2 IPC köprüsü.
 */

export interface ActiveSession {
  id: string;
  title: string;
  phase: string;
  phaseCode: string;
  startedAt: number;
  unit: string;
}

export interface PendingConfirm {
  id: string;
  title: string;
  confidence: number;
  createdAt: number;
}

export interface PendingApproval {
  id: string;
  title: string;
  summary: string;
  previewPath: string;
  sourceCount: number;
  verifiedQuotes: number;
  confidence: string;
  auditStatus: string;
  createdAt: number;
}

export interface RecentReport {
  id: string;
  title: string;
  completedAt: number;
  obsidianUri: string;
  sourceCount: number;
}

export interface DesktopState {
  voiceState: "idle" | "recording" | "paused" | "transcribing";
  recordingStartedAt: number;
  activeSessions: ActiveSession[];
  pendingConfirm: PendingConfirm[];
  pendingApproval: PendingApproval[];
  queuedOfflineCount: number;
  recentReports: RecentReport[];
}

export interface NoteItem {
  id: string;
  title: string;
  transcript: string;
  relPath: string;
  obsidianUri: string;
  intent: string;
  status: string;
  durationSecs: number;
  relatedResearch: string;
  createdAt: string;
}

export interface JobItem {
  id: string;
  noteId: string;
  topic: string;
  status: string;
  phase: string;
  phaseDetail: string;
  confidence: number;
  sourceCount: number;
  reportRelPath: string;
  obsidianUri: string;
  error: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface AppSnapshot {
  version: string;
  platform: "linux" | "macos" | "windows";
  needsOnboarding: boolean;
  vault: {
    path: string;
    name: string;
    exists: boolean;
    hafizaOs: boolean;
  };
  desktop: DesktopState;
  notes: NoteItem[];
  jobs: JobItem[];
}

export interface DiscoveredVault {
  name: string;
  path: string;
  isOpen: boolean;
  isHafizaOs: boolean;
}

export interface ReportPreview {
  jobId: string;
  status: string;
  previewPath: string;
  markdown: string;
  meta: {
    summary?: string;
    sourceCount?: number;
    verifiedQuotes?: number;
    confidence?: string;
    auditStatus?: string;
    title?: string;
    concepts?: string[];
  };
}

export interface SettingsPayload {
  configPath: string;
  vault: {
    path: string;
    vault_name: string;
    voice_dir: string;
    research_dir: string;
    hafiza_os_mode: "auto" | "enabled" | "disabled";
    personal_terms: string[];
    auto_update_maps: boolean;
  };
  audio: {
    capture_backend: "auto" | "pipewire" | "sounddevice";
    stt_provider: "local" | "groq" | "openai" | "custom";
    stt_endpoint: string;
    stt_model: string;
    stt_api_key_env: string;
    language: string;
    duck_enabled: boolean;
    cues_enabled: boolean;
    retention: string;
  };
  router: {
    backends: string[];
    auto_start_threshold: number;
    confirm_threshold: number;
  };
  research: {
    lead_backend: string;
    lead_model: string;
    worker_backend: string;
    worker_model: string;
    synth_backend: string;
    synth_model: string;
    audit_backend: string;
    audit_model: string;
    api_provider: "openai" | "anthropic";
    api_base_url: string;
    api_model: string;
    search_provider: "auto" | "tavily" | "brave" | "ddg";
    max_subagents: number;
    require_save_approval: boolean;
  };
  desktop: {
    enable_notifications: boolean;
    hotkey_toggle: string;
  };
  secrets: Record<string, boolean>;
}

export interface DoctorPayload {
  platform: string;
  vaultExists: boolean;
  hafizaOsDetected: boolean;
  captureBackend: string;
  sttProvider: string;
  bins: Record<string, boolean>;
  packages: Record<string, boolean>;
  secrets: Record<string, boolean>;
}

export interface BackendConnection {
  baseUrl: string;
  token: string;
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function resolveBackendConnection(): Promise<BackendConnection> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    const info = await invoke<{ port: number; token: string }>("get_backend_info");
    return {
      baseUrl: `http://127.0.0.1:${info.port}`,
      token: info.token,
    };
  }
  // Browser geliştirme modu için URL parametreleri veya VITE ortam değişkeni
  const params = new URLSearchParams(window.location.search);
  const port = params.get("port") || import.meta.env.VITE_SESLINOT_PORT || "8765";
  const token = params.get("token") || import.meta.env.VITE_SESLINOT_TOKEN || "dev";
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    token,
  };
}

export async function syncNativeHud(voiceState: DesktopState["voiceState"]): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const active = voiceState === "recording" || voiceState === "paused" || voiceState === "transcribing";
    await invoke("set_hud_visible", { visible: active });
    await invoke("set_tray_recording", { recording: voiceState === "recording" });
  } catch {
    // Non-fatal in dev
  }
}

export async function pickFolderDialog(defaultPath?: string): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({
      directory: true,
      multiple: false,
      defaultPath: defaultPath || undefined,
      title: "Obsidian Kasa Klasörünü Seç",
    });
    return typeof selected === "string" ? selected : null;
  } catch {
    return null;
  }
}

export class SesliNotApi {
  constructor(private conn: BackendConnection) {}

  private headers(): HeadersInit {
    return {
      Authorization: `Bearer ${this.conn.token}`,
      "Content-Type": "application/json",
    };
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${this.conn.baseUrl}${path}`, {
      ...init,
      headers: {
        ...this.headers(),
        ...(init?.headers || {}),
      },
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    return data as T;
  }

  getState(): Promise<AppSnapshot> {
    return this.request<AppSnapshot>("/api/state");
  }

  subscribeState(onState: (snap: AppSnapshot) => void, onError?: () => void): () => void {
    const url = `${this.conn.baseUrl}/api/events?token=${encodeURIComponent(this.conn.token)}`;
    const es = new EventSource(url);
    es.addEventListener("state", (ev) => {
      try {
        const parsed = JSON.parse((ev as MessageEvent).data) as AppSnapshot;
        onState(parsed);
      } catch {
        // Ignore malformed frame
      }
    });
    es.onerror = () => {
      onError?.();
    };
    return () => es.close();
  }

  recordAction(action: "toggle" | "start" | "stop" | "pause-toggle" | "cancel", forceResearch = false) {
    return this.request<{ ok: boolean; snapshot: AppSnapshot }>(`/api/record/${action}`, {
      method: "POST",
      body: JSON.stringify({ force_research: forceResearch }),
    });
  }

  createNote(text: string, opts?: { force_research?: boolean; quick?: boolean }) {
    return this.request<{ ok: boolean; vaultRelPath: string; jobId: string | null; snapshot: AppSnapshot }>(
      "/api/notes",
      {
        method: "POST",
        body: JSON.stringify({ text, ...opts }),
      }
    );
  }

  startResearch(topic: string, quick = false) {
    return this.request<{ ok: boolean; jobId: string | null; snapshot: AppSnapshot }>("/api/research", {
      method: "POST",
      body: JSON.stringify({ topic, quick }),
    });
  }

  getJobPreview(jobId: string): Promise<ReportPreview> {
    return this.request<ReportPreview>(`/api/jobs/${encodeURIComponent(jobId)}/preview`);
  }

  jobAction(jobId: string, action: "confirm" | "dismiss" | "cancel" | "approve-report" | "reject-report") {
    return this.request<{ ok: boolean; reportRelPath?: string; obsidianUri?: string; snapshot: AppSnapshot }>(
      `/api/jobs/${encodeURIComponent(jobId)}/${action}`,
      { method: "POST" }
    );
  }

  openTarget(target: string) {
    return this.request<{ ok: boolean }>("/api/open", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
  }

  listVaults() {
    return this.request<{ ok: boolean; vaults: DiscoveredVault[] }>("/api/vaults");
  }

  getDoctor() {
    return this.request<{ ok: boolean; doctor: DoctorPayload }>("/api/doctor");
  }

  getSettings() {
    return this.request<{ ok: boolean; settings: SettingsPayload }>("/api/settings");
  }

  updateSettings(payload: Record<string, unknown>) {
    return this.request<{ ok: boolean; settings: SettingsPayload; snapshot: AppSnapshot }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }

  completeOnboarding(payload: Record<string, unknown>) {
    return this.request<{ ok: boolean; settings: SettingsPayload; snapshot: AppSnapshot }>(
      "/api/onboarding/complete",
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  }
}

export function formatElapsed(startedEpoch: number, nowSec: number): string {
  if (!startedEpoch || startedEpoch <= 0) return "0:00";
  const diff = Math.max(0, Math.floor(nowSec - startedEpoch));
  const mins = Math.floor(diff / 60);
  const secs = diff % 60;
  return `${mins}:${secs < 10 ? "0" : ""}${secs}`;
}

export function formatShortTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString("tr-TR", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
