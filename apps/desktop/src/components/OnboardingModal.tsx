import { useEffect, useState } from "react";
import {
  ArrowRight,
  Check,
  FolderOpen,
  KeyRound,
  Mic,
  Sparkles,
} from "lucide-react";
import type { DiscoveredVault, SesliNotApi } from "../api";
import { pickFolderDialog } from "../api";

interface OnboardingModalProps {
  api: SesliNotApi;
  defaultVaultPath: string;
  platform: "linux" | "macos" | "windows";
  onCompleted: () => void;
}

export function OnboardingModal({
  api,
  defaultVaultPath,
  platform,
  onCompleted,
}: OnboardingModalProps) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [vaults, setVaults] = useState<DiscoveredVault[]>([]);
  const [vaultPath, setVaultPath] = useState(defaultVaultPath);
  const [sttProvider, setSttProvider] = useState<"groq" | "openai" | "local">(
    platform === "linux" ? "local" : "groq"
  );
  const [groqKey, setGroqKey] = useState("");
  const [researchMode, setResearchMode] = useState<"api" | "cli">(
    platform === "linux" ? "cli" : "api"
  );
  const [apiProvider, setApiProvider] = useState<"openai" | "anthropic">("openai");
  const [openaiKey, setOpenaiKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [requireApproval, setRequireApproval] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listVaults()
      .then((res) => {
        setVaults(res.vaults);
        if (res.vaults.length > 0) {
          setVaultPath(res.vaults[0].path);
        }
      })
      .catch(() => {});
  }, [api]);

  async function handleFinish() {
    setError("");
    setSaving(true);
    try {
      const secrets: Record<string, string> = {};
      if (groqKey.trim()) secrets.GROQ_API_KEY = groqKey.trim();
      if (openaiKey.trim()) secrets.OPENAI_API_KEY = openaiKey.trim();
      if (anthropicKey.trim()) secrets.ANTHROPIC_API_KEY = anthropicKey.trim();

      await api.completeOnboarding({
        vaultPath,
        sttProvider,
        researchMode,
        apiProvider,
        requireSaveApproval: requireApproval,
        secrets,
      });
      onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kurulum tamamlanamadı");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4 backdrop-blur-md">
      <div className="glass-card w-full max-w-xl overflow-hidden rounded-3xl border border-white/15">
        {/* Başlık */}
        <div className="border-b border-white/[0.08] bg-white/[0.02] px-6 py-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-amber-400 to-orange-600 text-zinc-950">
                <Sparkles className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-base font-bold text-white">SesliNot Hızlı Kurulum</h2>
                <p className="text-[12px] text-zinc-400">
                  Adım {step} / 3 — terminal gerekmeden 30 saniyede hazır
                </p>
              </div>
            </div>
            <div className="flex gap-1.5">
              {[1, 2, 3].map((n) => (
                <span
                  key={n}
                  className={`h-1.5 w-7 rounded-full transition ${
                    n <= step ? "bg-amber-400" : "bg-white/10"
                  }`}
                />
              ))}
            </div>
          </div>
        </div>

        {/* Gövde */}
        <div className="space-y-4 px-6 py-6">
          {step === 1 ? (
            <>
              <div className="text-sm font-semibold text-white">
                1. Obsidian Kasanı Seç
              </div>
              <p className="text-[12.5px] text-zinc-400">
                Sesli notlar <code className="text-amber-300">sesli-notlar/</code>, onayladığın
                araştırmalar <code className="text-emerald-300">araştırmalar/</code> klasörüne
                yazılır.
              </p>

              {vaults.length > 0 ? (
                <div className="space-y-2">
                  {vaults.map((v) => (
                    <button
                      key={v.path}
                      onClick={() => setVaultPath(v.path)}
                      className={`flex w-full items-center justify-between rounded-xl border px-3.5 py-2.5 text-left transition ${
                        vaultPath === v.path
                          ? "border-amber-400/50 bg-amber-500/10 text-white"
                          : "border-white/10 bg-white/[0.03] text-zinc-300 hover:border-white/20"
                      }`}
                    >
                      <div className="min-w-0">
                        <div className="text-[13.5px] font-semibold">
                          {v.name}{" "}
                          {v.isHafizaOs ? (
                            <span className="ml-1 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-300">
                              Hafıza OS
                            </span>
                          ) : null}
                        </div>
                        <div className="truncate font-mono text-[11px] text-zinc-500">{v.path}</div>
                      </div>
                      {vaultPath === v.path ? <Check className="h-4 w-4 text-amber-400" /> : null}
                    </button>
                  ))}
                </div>
              ) : null}

              <div className="flex gap-2">
                <input
                  type="text"
                  value={vaultPath}
                  onChange={(e) => setVaultPath(e.target.value)}
                  placeholder="Obsidian kasa klasör yolu"
                  className="flex-1 rounded-xl border border-white/10 bg-black/40 px-3.5 py-2.5 font-mono text-[12.5px] text-zinc-100 outline-none focus:border-amber-400/50"
                />
                <button
                  onClick={async () => {
                    const picked = await pickFolderDialog(vaultPath);
                    if (picked) setVaultPath(picked);
                  }}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.06] px-3.5 py-2 text-[12.5px] font-medium text-zinc-200 hover:bg-white/[0.12]"
                >
                  <FolderOpen className="h-4 w-4" />
                  Seç
                </button>
              </div>
            </>
          ) : null}

          {step === 2 ? (
            <>
              <div className="flex items-center gap-2 text-sm font-semibold text-white">
                <Mic className="h-4 w-4 text-amber-400" />
                2. Ses-Metin (STT) Motorunu Seç
              </div>
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                {(
                  [
                    ["groq", "Groq Whisper", "Ultra hızlı (<1sn), GPU gerekmez"],
                    ["openai", "OpenAI Whisper", "OPENAI_API_KEY ile bulut STT"],
                    ["local", "Yerel GPU", "whisper-server / voxtype (ücretsiz)"],
                  ] as const
                ).map(([id, title, desc]) => (
                  <button
                    key={id}
                    onClick={() => setSttProvider(id)}
                    className={`rounded-xl border p-3 text-left transition ${
                      sttProvider === id
                        ? "border-amber-400/50 bg-amber-500/10"
                        : "border-white/10 bg-white/[0.02] hover:border-white/20"
                    }`}
                  >
                    <div className="text-[13px] font-semibold text-white">{title}</div>
                    <div className="mt-1 text-[11.5px] text-zinc-400">{desc}</div>
                  </button>
                ))}
              </div>

              {sttProvider === "groq" ? (
                <div>
                  <label className="mb-1.5 block text-[12px] text-zinc-400">
                    GROQ_API_KEY (cihazında güvenle saklanır, kasaya yazılmaz)
                  </label>
                  <input
                    type="password"
                    value={groqKey}
                    onChange={(e) => setGroqKey(e.target.value)}
                    placeholder="gsk_..."
                    className="w-full rounded-xl border border-white/10 bg-black/40 px-3.5 py-2.5 font-mono text-[12.5px] text-zinc-100 outline-none focus:border-amber-400/50"
                  />
                </div>
              ) : null}
            </>
          ) : null}

          {step === 3 ? (
            <>
              <div className="flex items-center gap-2 text-sm font-semibold text-white">
                <KeyRound className="h-4 w-4 text-indigo-400" />
                3. Çok-Ajanlı Araştırma Motoru
              </div>

              <div className="grid grid-cols-2 gap-2.5">
                <button
                  onClick={() => setResearchMode("api")}
                  className={`rounded-xl border p-3.5 text-left transition ${
                    researchMode === "api"
                      ? "border-indigo-400/50 bg-indigo-500/10"
                      : "border-white/10 bg-white/[0.02]"
                  }`}
                >
                  <div className="text-[13px] font-semibold text-white">API Anahtarı (Önerilen)</div>
                  <div className="mt-1 text-[11.5px] text-zinc-400">
                    OpenAI veya Anthropic API key + canlı web araması
                  </div>
                </button>
                <button
                  onClick={() => setResearchMode("cli")}
                  className={`rounded-xl border p-3.5 text-left transition ${
                    researchMode === "cli"
                      ? "border-indigo-400/50 bg-indigo-500/10"
                      : "border-white/10 bg-white/[0.02]"
                  }`}
                >
                  <div className="text-[13px] font-semibold text-white">Yerel AI CLI</div>
                  <div className="mt-1 text-[11.5px] text-zinc-400">
                    Kurulu Claude Code / Codex / Agy aboneliğini kullan
                  </div>
                </button>
              </div>

              {researchMode === "api" ? (
                <div className="space-y-3">
                  <div className="flex gap-2">
                    {(["openai", "anthropic"] as const).map((p) => (
                      <button
                        key={p}
                        onClick={() => setApiProvider(p)}
                        className={`rounded-lg px-3 py-1.5 text-[12px] font-medium ${
                          apiProvider === p
                            ? "bg-white/15 text-white"
                            : "bg-white/[0.04] text-zinc-400"
                        }`}
                      >
                        {p === "openai" ? "OpenAI / OpenRouter" : "Anthropic Claude"}
                      </button>
                    ))}
                  </div>
                  <input
                    type="password"
                    value={apiProvider === "openai" ? openaiKey : anthropicKey}
                    onChange={(e) =>
                      apiProvider === "openai"
                        ? setOpenaiKey(e.target.value)
                        : setAnthropicKey(e.target.value)
                    }
                    placeholder={
                      apiProvider === "openai" ? "OPENAI_API_KEY (sk-...)" : "ANTHROPIC_API_KEY (sk-ant-...)"
                    }
                    className="w-full rounded-xl border border-white/10 bg-black/40 px-3.5 py-2.5 font-mono text-[12.5px] text-zinc-100 outline-none focus:border-indigo-400/50"
                  />
                </div>
              ) : null}

              <label className="flex cursor-pointer items-center justify-between rounded-xl border border-white/[0.08] bg-white/[0.025] px-3.5 py-3">
                <div>
                  <div className="text-[13px] font-medium text-white">
                    Kasaya kaydetmeden önce onayıma sun
                  </div>
                  <div className="text-[11.5px] text-zinc-400">
                    Raporlar önce önizleme ekranına düşer; onaylarsan kasaya yazılır.
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={requireApproval}
                  onChange={(e) => setRequireApproval(e.target.checked)}
                  className="h-4 w-4 accent-amber-400"
                />
              </label>
            </>
          ) : null}

          {error ? (
            <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3.5 py-2 text-[12.5px] text-rose-300">
              {error}
            </div>
          ) : null}
        </div>

        {/* Alt aksiyonlar */}
        <div className="flex items-center justify-between border-t border-white/[0.08] bg-black/25 px-6 py-4">
          <button
            disabled={step === 1}
            onClick={() => setStep((s) => (s > 1 ? ((s - 1) as 1 | 2 | 3) : s))}
            className="rounded-xl px-4 py-2 text-[13px] font-medium text-zinc-400 transition hover:text-white disabled:opacity-30"
          >
            Geri
          </button>

          {step < 3 ? (
            <button
              disabled={step === 1 && !vaultPath.trim()}
              onClick={() => setStep((s) => ((s + 1) as 1 | 2 | 3))}
              className="inline-flex items-center gap-1.5 rounded-xl bg-amber-400 px-5 py-2 text-[13px] font-semibold text-zinc-950 transition hover:bg-amber-300 disabled:opacity-40"
            >
              Devam Et
              <ArrowRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              disabled={saving}
              onClick={() => void handleFinish()}
              className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 px-5 py-2 text-[13px] font-semibold text-zinc-950 transition hover:brightness-110 disabled:opacity-50"
            >
              <Check className="h-4 w-4" />
              {saving ? "Kaydediliyor..." : "Kurulumu Tamamla"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
