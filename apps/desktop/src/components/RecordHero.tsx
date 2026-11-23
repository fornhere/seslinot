import { useState } from "react";
import {
  Compass,
  Loader2,
  Mic,
  Pause,
  Play,
  Send,
  Square,
  X,
} from "lucide-react";
import type { AppSnapshot } from "../api";
import { formatElapsed } from "../api";

interface RecordHeroProps {
  snapshot: AppSnapshot;
  nowSec: number;
  hotkey: string;
  busy: boolean;
  onRecordAction: (action: "toggle" | "pause-toggle" | "cancel", forceResearch?: boolean) => void;
  onQuickNote: (text: string, forceResearch: boolean) => Promise<void>;
}

const EQ_DELAYS = [0, 0.14, 0.28, 0.08, 0.22, 0.36, 0.12, 0.26, 0.05];

export function RecordHero({
  snapshot,
  nowSec,
  hotkey,
  busy,
  onRecordAction,
  onQuickNote,
}: RecordHeroProps) {
  const [quickText, setQuickText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const voice = snapshot.desktop.voiceState;
  const isRecording = voice === "recording";
  const isPaused = voice === "paused";
  const isTranscribing = voice === "transcribing";
  const isActive = isRecording || isPaused;

  async function handleSubmit(forceResearch: boolean) {
    const clean = quickText.trim();
    if (!clean || submitting) return;
    setSubmitting(true);
    try {
      await onQuickNote(clean, forceResearch);
      setQuickText("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="glass-card relative overflow-hidden rounded-2xl p-7">
      {/* Üst durum rozeti */}
      <div className="flex items-center justify-between">
        <div className="inline-flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.03] px-3 py-1 text-[12px] font-medium text-zinc-300">
          <span
            className={`h-2 w-2 rounded-full ${
              isRecording
                ? "animate-pulse bg-rose-500"
                : isPaused
                  ? "bg-amber-400"
                  : isTranscribing
                    ? "animate-pulse bg-sky-400"
                    : "bg-emerald-400"
            }`}
          />
          {isRecording
            ? "Mikrofon dinleniyor — cümlede “araştır” dersen çok-ajanlı oturum açılır"
            : isPaused
              ? "Kayıt duraklatıldı"
              : isTranscribing
                ? "Konuşma metne dönüştürülüyor ve kasaya yazılıyor..."
                : "Hazır — konuş, anında Obsidian notun oluşsun"}
        </div>
        <div className="hidden items-center gap-1.5 rounded-lg border border-white/[0.08] bg-black/30 px-2.5 py-1 font-mono text-[11px] text-zinc-400 sm:inline-flex">
          <span>Kısayol:</span>
          <span className="text-amber-300">{hotkey}</span>
        </div>
      </div>

      {/* Merkez Mikrofon Küresi */}
      <div className="my-7 flex flex-col items-center justify-center">
        <div className="relative flex items-center justify-center">
          {isRecording ? (
            <>
              <span className="orb-ring pointer-events-none absolute h-28 w-28 rounded-full border border-rose-500/50 bg-rose-500/10" />
              <span
                className="orb-ring pointer-events-none absolute h-28 w-28 rounded-full border border-amber-400/40"
                style={{ animationDelay: "0.7s" }}
              />
            </>
          ) : null}

          <button
            disabled={busy || isTranscribing}
            onClick={() => onRecordAction("toggle")}
            className={`relative z-10 flex h-24 w-24 items-center justify-center rounded-full transition-all duration-200 active:scale-95 disabled:opacity-60 ${
              isRecording
                ? "bg-gradient-to-br from-rose-500 via-red-500 to-orange-600 text-white shadow-2xl shadow-rose-500/35"
                : isPaused
                  ? "bg-gradient-to-br from-amber-400 to-orange-500 text-zinc-950 shadow-2xl shadow-amber-500/25"
                  : isTranscribing
                    ? "bg-gradient-to-br from-sky-500 to-indigo-600 text-white shadow-xl shadow-sky-500/25"
                    : "bg-gradient-to-br from-zinc-800 to-zinc-900 text-zinc-100 ring-1 ring-white/15 hover:from-amber-500/20 hover:to-orange-500/20 hover:text-amber-200 hover:ring-amber-400/40"
            }`}
            title={isActive ? "Kaydı durdur ve kaydet" : "Sesli not başlat"}
          >
            {isTranscribing ? (
              <Loader2 className="h-9 w-9 animate-spin" />
            ) : isActive ? (
              <Square className="h-8 w-8 fill-current" />
            ) : (
              <Mic className="h-9 w-9" />
            )}
          </button>
        </div>

        {/* Canlı süre & equalizer */}
        <div className="mt-5 flex min-h-9 flex-col items-center justify-center gap-2">
          {isActive ? (
            <>
              <div className="flex items-center gap-3">
                <div className="flex h-6 items-center gap-1">
                  {EQ_DELAYS.map((d, i) => (
                    <span
                      key={i}
                      className={`w-1 rounded-full ${
                        isRecording ? "eq-bar bg-amber-400" : "h-1.5 bg-zinc-600"
                      }`}
                      style={
                        isRecording
                          ? { height: `${14 + ((i * 7) % 12)}px`, animationDelay: `${d}s` }
                          : undefined
                      }
                    />
                  ))}
                </div>
                <span className="font-mono text-xl font-semibold tracking-tight text-white">
                  {formatElapsed(snapshot.desktop.recordingStartedAt, nowSec)}
                </span>
              </div>

              <div className="mt-2 flex items-center gap-2">
                <button
                  disabled={busy}
                  onClick={() => onRecordAction("pause-toggle")}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.05] px-3.5 py-1.5 text-[12.5px] font-medium text-zinc-200 transition hover:bg-white/[0.1]"
                >
                  {isPaused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
                  {isPaused ? "Devam Et" : "Duraklat"}
                </button>
                <button
                  disabled={busy}
                  onClick={() => onRecordAction("toggle", true)}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-indigo-400/30 bg-indigo-500/15 px-3.5 py-1.5 text-[12.5px] font-medium text-indigo-200 transition hover:bg-indigo-500/25"
                >
                  <Compass className="h-3.5 w-3.5" />
                  Bitir & Araştır
                </button>
                <button
                  disabled={busy}
                  onClick={() => onRecordAction("cancel")}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-rose-400/20 bg-rose-500/10 px-3 py-1.5 text-[12.5px] font-medium text-rose-300 transition hover:bg-rose-500/20"
                >
                  <X className="h-3.5 w-3.5" />
                  İptal
                </button>
              </div>
            </>
          ) : (
            <p className="text-center text-[13px] text-zinc-400">
              Mikrofona tıkla veya <span className="font-mono text-zinc-200">{hotkey}</span> tuşuna bas.
            </p>
          )}
        </div>
      </div>

      {/* Klavyeden Hızlı Not / Araştırma Çubuğu */}
      <div className="flex items-center gap-2 rounded-xl border border-white/[0.08] bg-black/35 p-2 focus-within:border-amber-400/40">
        <input
          type="text"
          value={quickText}
          onChange={(e) => setQuickText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void handleSubmit(false);
            }
          }}
          placeholder="Veya buraya yaz: hızlı not ekle ya da “SQLite WAL modunu araştır”..."
          className="flex-1 bg-transparent px-2.5 py-1.5 text-[13.5px] text-zinc-100 placeholder-zinc-500 outline-none"
        />
        <button
          disabled={submitting || !quickText.trim()}
          onClick={() => void handleSubmit(false)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-white/[0.08] px-3 py-1.5 text-[12.5px] font-medium text-zinc-200 transition hover:bg-white/[0.14] disabled:opacity-40"
        >
          <Send className="h-3.5 w-3.5" />
          Not
        </button>
        <button
          disabled={submitting || !quickText.trim()}
          onClick={() => void handleSubmit(true)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-indigo-500 to-sky-500 px-3.5 py-1.5 text-[12.5px] font-semibold text-white shadow-md shadow-indigo-500/20 transition hover:brightness-110 disabled:opacity-40"
        >
          <Compass className="h-3.5 w-3.5" />
          Araştır
        </button>
      </div>
    </div>
  );
}
