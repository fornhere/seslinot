import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import {
  SesliNotApi,
  isTauriRuntime,
  resolveBackendConnection,
  syncNativeHud,
  type AppSnapshot,
} from "./api";
import { ApprovalDeck } from "./components/ApprovalDeck";
import { FloatingHud } from "./components/FloatingHud";
import { OnboardingModal } from "./components/OnboardingModal";
import { RecordHero } from "./components/RecordHero";
import { ResearchLive } from "./components/ResearchLive";
import { Sidebar, type NavTab } from "./components/Sidebar";
import { SettingsView } from "./components/SettingsView";
import { VaultFeed } from "./components/VaultFeed";

export default function App() {
  const isHudMode =
    typeof window !== "undefined" &&
    (window.location.hash === "#hud" ||
      new URLSearchParams(window.location.search).get("mode") === "hud");

  const [api, setApi] = useState<SesliNotApi | null>(null);
  const [snapshot, setSnapshot] = useState<AppSnapshot | null>(null);
  const [tab, setTab] = useState<NavTab>("studio");
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  const [busy, setBusy] = useState(false);
  const [hotkey, setHotkey] = useState("Ctrl+Alt+N");
  const [toast, setToast] = useState<{ text: string; kind: "ok" | "err" } | null>(null);
  const [bootError, setBootError] = useState("");
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((text: string, kind: "ok" | "err" = "ok") => {
    setToast({ text, kind });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  // Canlı süre sayacı
  useEffect(() => {
    const id = window.setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Backend bağlantısı + SSE aboneliği
  useEffect(() => {
    let cleanupSse: (() => void) | null = null;
    let cancelled = false;

    async function boot() {
      for (let attempt = 0; attempt < 15; attempt++) {
        try {
          const conn = await resolveBackendConnection();
          const client = new SesliNotApi(conn);
          const initial = await client.getState();
          if (cancelled) return;
          setApi(client);
          setSnapshot(initial);
          if (!isHudMode) {
            client
              .getSettings()
              .then((s) => {
                if (!cancelled && s.settings.desktop.hotkey_toggle) {
                  setHotkey(s.settings.desktop.hotkey_toggle);
                }
              })
              .catch(() => {});
          }
          cleanupSse = client.subscribeState((snap) => {
            if (!cancelled) setSnapshot(snap);
          });
          return;
        } catch (err) {
          if (attempt === 14 && !cancelled) {
            setBootError(
              err instanceof Error
                ? err.message
                : "SesliNot yerel motoruna bağlanılamadı."
            );
          }
          await new Promise((r) => setTimeout(r, 400));
        }
      }
    }

    void boot();
    return () => {
      cancelled = true;
      cleanupSse?.();
    };
  }, [isHudMode]);

  // Native Floating HUD & Tray senkronu (sadece ana pencereden tetiklenir)
  useEffect(() => {
    if (!isHudMode && snapshot) {
      void syncNativeHud(snapshot.desktop.voiceState);
    }
  }, [isHudMode, snapshot?.desktop.voiceState]);

  const handleRecordAction = useCallback(
    async (action: "toggle" | "pause-toggle" | "cancel", forceResearch = false) => {
      if (!api || busy) return;
      setBusy(true);
      try {
        const res = await api.recordAction(action, forceResearch);
        setSnapshot(res.snapshot);
      } catch (err) {
        showToast(err instanceof Error ? err.message : "Kayıt işlemi başarısız", "err");
      } finally {
        setBusy(false);
      }
    },
    [api, busy, showToast]
  );

  // Tauri global kısayol ve tepsi olaylarını dinle
  useEffect(() => {
    if (isHudMode || !isTauriRuntime() || !api) return;
    let unlistenToggle: (() => void) | null = null;
    let unlistenTab: (() => void) | null = null;

    import("@tauri-apps/api/event")
      .then(async ({ listen }) => {
        unlistenToggle = await listen("seslinot://toggle-record", () => {
          void handleRecordAction("toggle");
        });
        unlistenTab = await listen<string>("seslinot://open-tab", (ev) => {
          if (ev.payload === "approvals" || ev.payload === "studio" || ev.payload === "settings") {
            setTab(ev.payload);
          }
        });
      })
      .catch(() => {});

    return () => {
      unlistenToggle?.();
      unlistenTab?.();
    };
  }, [isHudMode, api, handleRecordAction]);

  // --- Floating HUD penceresi ---
  if (isHudMode) {
    return (
      <FloatingHud
        snapshot={snapshot}
        nowSec={nowSec}
        onAction={(act) => void handleRecordAction(act)}
      />
    );
  }

  // --- Açılış / Hata ekranı ---
  if (!snapshot || !api) {
    return (
      <div className="studio-bg flex h-screen w-screen flex-col items-center justify-center gap-3 p-6 text-center">
        {bootError ? (
          <>
            <AlertCircle className="h-9 w-9 text-rose-400" />
            <div className="text-base font-semibold text-white">Yerel motor başlatılamadı</div>
            <p className="max-w-md font-mono text-xs text-zinc-400">{bootError}</p>
          </>
        ) : (
          <>
            <Loader2 className="h-8 w-8 animate-spin text-amber-400" />
            <div className="text-sm font-medium text-zinc-300">SesliNot motoru hazırlanıyor...</div>
          </>
        )}
      </div>
    );
  }

  const isRecording = snapshot.desktop.voiceState === "recording";

  return (
    <div
      className={`flex h-screen w-screen overflow-hidden text-zinc-100 ${
        isRecording ? "studio-bg-recording" : "studio-bg"
      }`}
    >
      <Sidebar
        active={tab}
        onSelect={setTab}
        snapshot={snapshot}
        onOpenVault={() => void api.openTarget(snapshot.vault.path)}
      />

      <main className="relative flex-1 overflow-y-auto px-7 py-6">
        {/* Toast bildirimi */}
        {toast ? (
          <div
            className={`fixed right-6 bottom-6 z-40 flex items-center gap-2 rounded-xl border px-4 py-2.5 text-[13px] font-medium shadow-2xl backdrop-blur-md ${
              toast.kind === "err"
                ? "border-rose-500/35 bg-rose-950/90 text-rose-200"
                : "border-emerald-500/35 bg-emerald-950/90 text-emerald-200"
            }`}
          >
            {toast.kind === "err" ? (
              <AlertCircle className="h-4 w-4" />
            ) : (
              <CheckCircle2 className="h-4 w-4" />
            )}
            {toast.text}
          </div>
        ) : null}

        {tab === "studio" ? (
          <div className="mx-auto flex max-w-4xl flex-col gap-6">
            <RecordHero
              snapshot={snapshot}
              nowSec={nowSec}
              hotkey={hotkey}
              busy={busy}
              onRecordAction={(a, f) => void handleRecordAction(a, f)}
              onQuickNote={async (text, forceResearch) => {
                const res = await api.createNote(text, { force_research: forceResearch });
                setSnapshot(res.snapshot);
                showToast(
                  res.jobId
                    ? "Not kaydedildi ve çok-ajanlı araştırma kuyruğa alındı!"
                    : "Not Obsidian kasana kaydedildi!"
                );
              }}
            />

            {/* Bekleyen Onay Öne Çıkan Banner */}
            {snapshot.desktop.pendingApproval.length > 0 ? (
              <button
                onClick={() => setTab("approvals")}
                className="flex items-center justify-between rounded-2xl border border-amber-400/35 bg-gradient-to-r from-amber-500/15 via-orange-500/10 to-transparent px-5 py-4 text-left transition hover:border-amber-400/60"
              >
                <div>
                  <div className="text-[13.5px] font-semibold text-amber-200">
                    {snapshot.desktop.pendingApproval.length} araştırma raporu kasaya kayıt onayı
                    bekliyor
                  </div>
                  <div className="mt-0.5 text-[12px] text-zinc-400">
                    Önizle, kaynakları incele ve tek tıkla Obsidian kasana kaydet.
                  </div>
                </div>
                <span className="rounded-xl bg-amber-400 px-3.5 py-1.5 text-[12.5px] font-semibold text-zinc-950">
                  İncele →
                </span>
              </button>
            ) : null}

            <VaultFeed snapshot={snapshot} onOpenUri={(uri) => void api.openTarget(uri)} />
          </div>
        ) : null}

        {tab === "approvals" ? (
          <ApprovalDeck
            api={api}
            items={snapshot.desktop.pendingApproval}
            onApprove={async (jobId) => {
              const res = await api.jobAction(jobId, "approve-report");
              setSnapshot(res.snapshot);
              showToast("Rapor onaylandı ve Obsidian kasana kaydedildi!");
            }}
            onReject={async (jobId) => {
              const res = await api.jobAction(jobId, "reject-report");
              setSnapshot(res.snapshot);
              showToast("Rapor reddedildi — kasaya dokunulmadı.");
            }}
            onOpenExternal={(p) => void api.openTarget(p)}
          />
        ) : null}

        {tab === "research" ? (
          <div className="mx-auto max-w-4xl">
            <ResearchLive
              snapshot={snapshot}
              nowSec={nowSec}
              onJobAction={async (jobId, action) => {
                const res = await api.jobAction(jobId, action);
                setSnapshot(res.snapshot);
              }}
              onStartResearch={async (topic, quick) => {
                const res = await api.startResearch(topic, quick);
                setSnapshot(res.snapshot);
              }}
            />
          </div>
        ) : null}

        {tab === "vault" ? (
          <div className="mx-auto max-w-4xl">
            <VaultFeed snapshot={snapshot} onOpenUri={(uri) => void api.openTarget(uri)} />
          </div>
        ) : null}

        {tab === "settings" ? (
          <div className="mx-auto max-w-4xl">
            <SettingsView
              api={api}
              onSaved={() => {
                api.getState().then(setSnapshot).catch(() => {});
              }}
            />
          </div>
        ) : null}
      </main>

      {snapshot.needsOnboarding ? (
        <OnboardingModal
          api={api}
          defaultVaultPath={snapshot.vault.path}
          platform={snapshot.platform}
          onCompleted={() => {
            api.getState().then(setSnapshot).catch(() => {});
            showToast("SesliNot kurulumu tamamlandı!");
          }}
        />
      ) : null}
    </div>
  );
}
