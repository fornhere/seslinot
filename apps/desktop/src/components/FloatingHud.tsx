import { Loader2, Pause, Play, Square, X } from "lucide-react";
import type { AppSnapshot } from "../api";
import { formatElapsed } from "../api";

interface FloatingHudProps {
  snapshot: AppSnapshot | null;
  nowSec: number;
  onAction: (action: "toggle" | "pause-toggle" | "cancel") => void;
}

export function FloatingHud({ snapshot, nowSec, onAction }: FloatingHudProps) {
  const voice = snapshot?.desktop.voiceState ?? "idle";
  const isRecording = voice === "recording";
  const isPaused = voice === "paused";
  const isTranscribing = voice === "transcribing";

  return (
    <div
      data-tauri-drag-region
      className="flex h-screen w-screen items-center justify-center bg-transparent p-1.5 select-none"
    >
      <div
        data-tauri-drag-region
        className="glass-pill flex h-[52px] w-full items-center justify-between rounded-full px-3.5"
      >
        <div className="flex items-center gap-2.5" data-tauri-drag-region>
          {isTranscribing ? (
            <Loader2 className="h-4 w-4 animate-spin text-sky-400" />
          ) : (
            <span
              className={`h-2.5 w-2.5 rounded-full ${
                isRecording ? "animate-pulse bg-rose-500 shadow-sm shadow-rose-500" : "bg-amber-400"
              }`}
            />
          )}

          {isRecording ? (
            <div className="flex h-5 items-center gap-0.5">
              {[0, 0.15, 0.3, 0.1, 0.25].map((delay, idx) => (
                <span
                  key={idx}
                  className="eq-bar w-0.5 rounded-full bg-amber-400"
                  style={{ height: `${10 + ((idx * 5) % 10)}px`, animationDelay: `${delay}s` }}
                />
              ))}
            </div>
          ) : null}

          <span className="font-mono text-[13px] font-semibold text-white">
            {isTranscribing
              ? "Yazıya dökülüyor..."
              : isPaused
                ? "Duraklatıldı"
                : formatElapsed(snapshot?.desktop.recordingStartedAt ?? 0, nowSec)}
          </span>
        </div>

        {!isTranscribing ? (
          <div className="flex items-center gap-1">
            <button
              onClick={() => onAction("pause-toggle")}
              className="flex h-7 w-7 items-center justify-center rounded-full bg-white/[0.08] text-zinc-200 transition hover:bg-white/[0.16]"
              title={isPaused ? "Devam Et" : "Duraklat"}
            >
              {isPaused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            </button>
            <button
              onClick={() => onAction("toggle")}
              className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/90 text-zinc-950 transition hover:bg-emerald-400"
              title="Durdur ve Kaydet"
            >
              <Square className="h-3 w-3 fill-current" />
            </button>
            <button
              onClick={() => onAction("cancel")}
              className="flex h-7 w-7 items-center justify-center rounded-full bg-rose-500/20 text-rose-300 transition hover:bg-rose-500/35"
              title="İptal Et"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
