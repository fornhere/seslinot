import { useEffect, useState } from "react";
import { Check, FolderOpen, KeyRound, RefreshCw, Save, ShieldCheck } from "lucide-react";
import type { DoctorPayload, SesliNotApi, SettingsPayload } from "../api";
import { pickFolderDialog } from "../api";

interface SettingsViewProps {
  api: SesliNotApi;
  onSaved: () => void;
}

const SECRET_LABELS: { key: string; label: string; placeholder: string }[] = [
  { key: "GROQ_API_KEY", label: "Groq Whisper API Anahtarı", placeholder: "gsk_..." },
  { key: "OPENAI_API_KEY", label: "OpenAI API Anahtarı", placeholder: "sk-..." },
  { key: "ANTHROPIC_API_KEY", label: "Anthropic Claude API Anahtarı", placeholder: "sk-ant-..." },
  { key: "TAVILY_API_KEY", label: "Tavily Web Arama Anahtarı (opsiyonel)", placeholder: "tvly-..." },
];

export function SettingsView({ api, onSaved }: SettingsViewProps) {
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null);
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");

  async function loadAll() {
    const [s, d] = await Promise.all([api.getSettings(), api.getDoctor()]);
    setSettings(s.settings);
    setDoctor(d.doctor);
  }

  useEffect(() => {
    void loadAll();
  }, []);

  if (!settings) {
    return <div className="p-6 text-sm text-zinc-400">Ayarlar yükleniyor...</div>;
  }

  async function handleSave() {
    if (!settings) return;
    setSaving(true);
    setToast("");
    try {
      const secretsPatch: Record<string, string> = {};
      for (const [k, v] of Object.entries(secretInputs)) {
        if (v.trim()) secretsPatch[k] = v.trim();
      }
      const res = await api.updateSettings({
        vault: settings.vault,
        audio: settings.audio,
        research: settings.research,
        desktop: settings.desktop,
        secrets: secretsPatch,
      });
      setSettings(res.settings);
      setSecretInputs({});
      setToast("Ayarlar kaydedildi ve anında uygulandı.");
      onSaved();
    } catch (err) {
      setToast(err instanceof Error ? err.message : "Ayarlar kaydedilemedi");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-5 pb-10">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Ayarlar & Sistem Sağlığı</h2>
          <p className="font-mono text-[11.5px] text-zinc-500">{settings.configPath}</p>
        </div>
        <button
          disabled={saving}
          onClick={() => void handleSave()}
          className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-amber-400 to-orange-500 px-4 py-2 text-[13px] font-semibold text-zinc-950 shadow-lg shadow-amber-500/20 transition hover:brightness-110 disabled:opacity-50"
        >
          <Save className="h-4 w-4" />
          {saving ? "Kaydediliyor..." : "Değişiklikleri Kaydet"}
        </button>
      </div>

      {toast ? (
        <div className="rounded-xl border border-emerald-400/25 bg-emerald-500/10 px-4 py-2.5 text-[13px] text-emerald-200">
          {toast}
        </div>
      ) : null}

      {/* Kasa ve Kısayol */}
      <div className="glass-card space-y-4 rounded-2xl p-5">
        <h3 className="text-sm font-semibold text-white">Obsidian Kasası & Onay Kapısı</h3>
        <div className="flex gap-2">
          <input
            type="text"
            value={settings.vault.path}
            onChange={(e) =>
              setSettings({ ...settings, vault: { ...settings.vault, path: e.target.value } })
            }
            className="flex-1 rounded-xl border border-white/10 bg-black/40 px-3.5 py-2 font-mono text-[12.5px] text-zinc-100 outline-none focus:border-amber-400/50"
          />
          <button
            onClick={async () => {
              const picked = await pickFolderDialog(settings.vault.path);
              if (picked) {
                setSettings({ ...settings, vault: { ...settings.vault, path: picked } });
              }
            }}
            className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.06] px-3 py-2 text-[12.5px] text-zinc-200 hover:bg-white/[0.12]"
          >
            <FolderOpen className="h-4 w-4" />
            Gözat
          </button>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[12px] text-zinc-400">STT Sağlayıcı</label>
            <select
              value={settings.audio.stt_provider}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  audio: {
                    ...settings.audio,
                    stt_provider: e.target.value as SettingsPayload["audio"]["stt_provider"],
                  },
                })
              }
              className="w-full rounded-xl border border-white/10 bg-[#0d1018] px-3 py-2 text-[13px] text-zinc-100 outline-none"
            >
              <option value="local">Yerel GPU (whisper-server / voxtype)</option>
              <option value="groq">Groq Cloud Whisper (Ultra Hızlı)</option>
              <option value="openai">OpenAI Whisper API</option>
              <option value="custom">Özel OpenAI-uyumlu Uç Nokta</option>
            </select>
          </div>

          <div>
            <label className="mb-1 block text-[12px] text-zinc-400">Global Kısayol</label>
            <input
              type="text"
              value={settings.desktop.hotkey_toggle}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  desktop: { ...settings.desktop, hotkey_toggle: e.target.value },
                })
              }
              className="w-full rounded-xl border border-white/10 bg-black/40 px-3 py-2 font-mono text-[12.5px] text-zinc-100 outline-none"
            />
          </div>
        </div>

        <label className="flex cursor-pointer items-center justify-between rounded-xl border border-white/[0.07] bg-white/[0.02] px-4 py-3">
          <div>
            <div className="text-[13px] font-medium text-white">
              Araştırma raporlarını kaydetmeden önce onayıma sun
            </div>
            <div className="text-[11.5px] text-zinc-400">
              Açıkken raporlar önce “Onay Bekleyenler” ekranında önizlenir.
            </div>
          </div>
          <input
            type="checkbox"
            checked={settings.research.require_save_approval}
            onChange={(e) =>
              setSettings({
                ...settings,
                research: { ...settings.research, require_save_approval: e.target.checked },
              })
            }
            className="h-4 w-4 accent-amber-400"
          />
        </label>
      </div>

      {/* API Anahtarları */}
      <div className="glass-card space-y-3 rounded-2xl p-5">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <KeyRound className="h-4 w-4 text-amber-400" />
          API Anahtarları (yalnız cihazındaki 0600 secrets.env dosyasında tutulur)
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {SECRET_LABELS.map((item) => {
            const configured = settings.secrets[item.key];
            return (
              <div key={item.key}>
                <div className="mb-1 flex items-center justify-between text-[12px]">
                  <span className="text-zinc-400">{item.label}</span>
                  {configured ? (
                    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-400">
                      <Check className="h-3 w-3" /> Tanımlı
                    </span>
                  ) : (
                    <span className="text-[11px] text-zinc-600">Boş</span>
                  )}
                </div>
                <input
                  type="password"
                  value={secretInputs[item.key] ?? ""}
                  onChange={(e) =>
                    setSecretInputs({ ...secretInputs, [item.key]: e.target.value })
                  }
                  placeholder={configured ? "•••••••• (değiştirmek için yaz)" : item.placeholder}
                  className="w-full rounded-xl border border-white/10 bg-black/40 px-3 py-2 font-mono text-[12px] text-zinc-100 outline-none focus:border-amber-400/40"
                />
              </div>
            );
          })}
        </div>
      </div>

      {/* Doctor Sağlık Özeti */}
      {doctor ? (
        <div className="glass-card space-y-3 rounded-2xl p-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <ShieldCheck className="h-4 w-4 text-emerald-400" />
              Ortam Sağlık Kontrolü ({doctor.platform})
            </div>
            <button
              onClick={() => void loadAll()}
              className="inline-flex items-center gap-1 text-[12px] text-zinc-400 hover:text-white"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Yenile
            </button>
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(doctor.bins).map(([name, ok]) => (
              <span
                key={name}
                className={`rounded-lg border px-2.5 py-1 font-mono text-[11px] ${
                  ok
                    ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
                    : "border-white/[0.06] bg-white/[0.02] text-zinc-500"
                }`}
              >
                {ok ? "✓" : "·"} {name}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
