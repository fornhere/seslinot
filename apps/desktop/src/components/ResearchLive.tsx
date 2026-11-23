import {
  Check,
  Compass,
  Loader2,
  PlayCircle,
  WifiOff,
  X,
  XCircle,
} from "lucide-react";
import type { AppSnapshot } from "../api";
import { formatElapsed } from "../api";

interface ResearchLiveProps {
  snapshot: AppSnapshot;
  nowSec: number;
  onJobAction: (jobId: string, action: "confirm" | "dismiss" | "cancel") => Promise<void>;
  onStartResearch: (topic: string, quick: boolean) => Promise<void>;
}

const STAGES = [
  { code: "VAULT_SCAN", label: "Kasa" },
  { code: "PLANNING", label: "Planlama" },
  { code: "GATHERING", label: "Alt-Ajanlar" },
  { code: "VERIFYING", label: "Span.verify" },
  { code: "SYNTHESIZING", label: "Sentez" },
  { code: "AUDITING", label: "Denetim" },
  { code: "AWAITING_APPROVAL", label: "Onay" },
];

function stageIndex(code: string): number {
  const idx = STAGES.findIndex((s) => s.code === code);
  return idx >= 0 ? idx : 0;
}

export function ResearchLive({ snapshot, nowSec, onJobAction }: ResearchLiveProps) {
  const { activeSessions, pendingConfirm, queuedOfflineCount } = snapshot.desktop;

  return (
    <div className="flex flex-col gap-5">
      {queuedOfflineCount > 0 ? (
        <div className="flex items-center gap-3 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-[13px] text-amber-200">
          <WifiOff className="h-4 w-4 shrink-0 text-amber-400" />
          <span>
            Çevrimdışı kuyrukta <strong>{queuedOfflineCount}</strong> iş internet bekliyor. Bağlantı
            geldiğinde otomatik devam edecek.
          </span>
        </div>
      ) : null}

      {/* Orta güven onay bekleyenler */}
      {pendingConfirm.length > 0 ? (
        <section className="flex flex-col gap-2.5">
          <h3 className="px-1 text-[11.5px] font-semibold uppercase tracking-wider text-zinc-400">
            Başlatma Onayı Bekleyenler ({pendingConfirm.length})
          </h3>
          {pendingConfirm.map((item) => (
            <div
              key={item.id}
              className="glass-card flex flex-wrap items-center justify-between gap-3 rounded-xl p-4"
            >
              <div>
                <div className="text-[14px] font-semibold text-white">{item.title}</div>
                <div className="mt-0.5 text-[12px] text-zinc-400">
                  Araştırma niyeti güveni:{" "}
                  <span className="font-mono text-amber-300">
                    %{Math.round(item.confidence * 100)}
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => void onJobAction(item.id, "confirm")}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-indigo-500 px-3.5 py-2 text-[12.5px] font-semibold text-white transition hover:bg-indigo-400"
                >
                  <Check className="h-3.5 w-3.5" />
                  Araştırmayı Başlat
                </button>
                <button
                  onClick={() => void onJobAction(item.id, "dismiss")}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2 text-[12.5px] font-medium text-zinc-300 transition hover:bg-white/[0.09]"
                >
                  <X className="h-3.5 w-3.5" />
                  Sadece Not Kalsın
                </button>
              </div>
            </div>
          ))}
        </section>
      ) : null}

      {/* Aktif çok-ajanlı oturumlar */}
      <section className="flex flex-col gap-3">
        <h3 className="px-1 text-[11.5px] font-semibold uppercase tracking-wider text-zinc-400">
          Aktif Çok-Ajanlı Araştırmalar ({activeSessions.length})
        </h3>
        {activeSessions.length === 0 ? (
          <div className="glass-card flex flex-col items-center justify-center rounded-2xl px-6 py-14 text-center">
            <Compass className="mb-3 h-8 w-8 text-indigo-400/70" />
            <div className="text-[15px] font-semibold text-zinc-200">Aktif araştırma oturumu yok</div>
            <p className="mt-1.5 max-w-md text-[13px] text-zinc-400">
              Konuşurken cümlende “araştır” dediğinde veya Kayıt Stüdyosu’ndaki “Araştır” düğmesine
              bastığında çok-ajanlı hat burada canlı takip edilir.
            </p>
          </div>
        ) : (
          activeSessions.map((sess) => {
            const currentIdx = stageIndex(sess.phaseCode);
            return (
              <div key={sess.id} className="glass-card rounded-2xl p-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin text-indigo-400" />
                      <span className="text-[15px] font-semibold text-white">{sess.title}</span>
                    </div>
                    <p className="mt-1 text-[12.5px] text-indigo-300">
                      {sess.phase} ·{" "}
                      <span className="font-mono text-zinc-400">
                        {formatElapsed(sess.startedAt, nowSec)}
                      </span>
                    </p>
                  </div>
                  <button
                    onClick={() => void onJobAction(sess.id, "cancel")}
                    className="inline-flex items-center gap-1 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-[12px] font-medium text-rose-300 transition hover:bg-rose-500/20"
                  >
                    <XCircle className="h-3.5 w-3.5" />
                    İptal
                  </button>
                </div>

                {/* 7 Aşamalı Görsel Boru Hattı */}
                <div className="mt-5 grid grid-cols-7 gap-1.5">
                  {STAGES.map((stage, idx) => {
                    const done = idx < currentIdx;
                    const active = idx === currentIdx;
                    return (
                      <div key={stage.code} className="flex flex-col gap-1.5">
                        <div
                          className={`h-1.5 rounded-full transition-all ${
                            done
                              ? "bg-emerald-400"
                              : active
                                ? "animate-pulse bg-gradient-to-r from-indigo-400 to-sky-400"
                                : "bg-white/[0.08]"
                          }`}
                        />
                        <span
                          className={`truncate text-center text-[10.5px] font-medium ${
                            done
                              ? "text-emerald-300"
                              : active
                                ? "text-sky-300"
                                : "text-zinc-600"
                          }`}
                        >
                          {stage.label}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })
        )}
      </section>

      {/* Son iş geçmişi özeti */}
      {snapshot.jobs.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="px-1 text-[11.5px] font-semibold uppercase tracking-wider text-zinc-500">
            Son Araştırma Geçmişi
          </h3>
          <div className="glass-card divide-y divide-white/[0.06] rounded-2xl">
            {snapshot.jobs.slice(0, 8).map((j) => (
              <div key={j.id} className="flex items-center justify-between gap-3 px-4 py-3 text-[13px]">
                <div className="min-w-0">
                  <div className="truncate font-medium text-zinc-200">{j.topic}</div>
                  <div className="mt-0.5 text-[11.5px] text-zinc-500">
                    {j.phaseDetail || j.phase}
                    {j.sourceCount > 0 ? ` · ${j.sourceCount} kaynak` : ""}
                  </div>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-0.5 font-mono text-[11px] font-semibold ${
                    j.status === "SUCCEEDED"
                      ? "bg-emerald-500/15 text-emerald-300"
                      : j.status === "AWAITING_APPROVAL"
                        ? "bg-amber-500/15 text-amber-300"
                        : j.status === "RUNNING"
                          ? "bg-indigo-500/15 text-indigo-300"
                          : j.status === "FAILED"
                            ? "bg-rose-500/15 text-rose-300"
                            : "bg-white/[0.06] text-zinc-400"
                  }`}
                >
                  {j.status}
                </span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
