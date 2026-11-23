import { useState } from "react";
import { BookOpen, ExternalLink, Mic, Search, Sparkles } from "lucide-react";
import type { AppSnapshot } from "../api";
import { formatShortTime } from "../api";

interface VaultFeedProps {
  snapshot: AppSnapshot;
  onOpenUri: (uri: string) => void;
}

export function VaultFeed({ snapshot, onOpenUri }: VaultFeedProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "notes" | "reports">("all");
  const q = query.trim().toLowerCase();

  const reports = snapshot.desktop.recentReports.filter(
    (r) => !q || r.title.toLowerCase().includes(q)
  );
  const notes = snapshot.notes.filter(
    (n) =>
      !q ||
      n.title.toLowerCase().includes(q) ||
      n.transcript.toLowerCase().includes(q)
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-1 items-center gap-2 rounded-xl border border-white/[0.08] bg-black/35 px-3 py-2 focus-within:border-amber-400/40">
          <Search className="h-4 w-4 text-zinc-500" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Sesli notlarda veya araştırma raporlarında ara..."
            className="flex-1 bg-transparent text-[13.5px] text-zinc-100 placeholder-zinc-500 outline-none"
          />
        </div>

        <div className="flex items-center rounded-xl border border-white/[0.08] bg-white/[0.03] p-1 text-[12px]">
          {(
            [
              ["all", "Tümü"],
              ["reports", "Raporlar"],
              ["notes", "Sesli Notlar"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              onClick={() => setFilter(id)}
              className={`rounded-lg px-3 py-1 font-medium transition ${
                filter === id ? "bg-white/[0.1] text-white" : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Onaylı Araştırma Raporları */}
      {filter !== "notes" && reports.length > 0 ? (
        <section className="flex flex-col gap-2">
          <div className="px-1 text-[11px] font-semibold uppercase tracking-wider text-emerald-400/90">
            Kasaya Kaydedilen Raporlar ({reports.length})
          </div>
          <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
            {reports.map((rep) => (
              <button
                key={rep.id}
                onClick={() => onOpenUri(rep.obsidianUri)}
                className="glass-card group flex items-center justify-between gap-3 rounded-xl p-4 text-left transition hover:border-emerald-400/35"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 text-[11px] font-medium text-emerald-300">
                    <BookOpen className="h-3.5 w-3.5" />
                    <span>{rep.sourceCount} doğrulanmış kaynak</span>
                  </div>
                  <div className="mt-1 truncate text-[14px] font-semibold text-white">
                    {rep.title}
                  </div>
                </div>
                <ExternalLink className="h-4 w-4 shrink-0 text-zinc-500 transition group-hover:text-emerald-300" />
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {/* Sesli Notlar */}
      {filter !== "reports" ? (
        <section className="flex flex-col gap-2">
          <div className="px-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
            Son Sesli Notlar ({notes.length})
          </div>
          {notes.length === 0 ? (
            <div className="glass-card flex flex-col items-center justify-center rounded-2xl py-14 text-center text-zinc-500">
              <Sparkles className="mb-2 h-6 w-6" />
              Henüz kayıtlı not bulunamadı.
            </div>
          ) : (
            <div className="glass-card divide-y divide-white/[0.06] rounded-2xl">
              {notes.map((n) => (
                <div
                  key={n.id}
                  className="group flex items-start justify-between gap-4 px-4 py-3.5 transition hover:bg-white/[0.02]"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <Mic className="h-3.5 w-3.5 shrink-0 text-amber-400/80" />
                      <span className="truncate text-[14px] font-semibold text-zinc-100">
                        {n.title}
                      </span>
                      <span className="rounded-md bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10.5px] text-zinc-400">
                        {n.status}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[12.5px] leading-relaxed text-zinc-400">
                      {n.transcript}
                    </p>
                    <div className="mt-1.5 flex items-center gap-3 text-[11px] text-zinc-500">
                      <span>{formatShortTime(n.createdAt)}</span>
                      {n.durationSecs > 0 ? <span>{n.durationSecs} sn</span> : null}
                      <span className="truncate font-mono text-zinc-600">{n.relPath}</span>
                    </div>
                  </div>
                  {n.obsidianUri ? (
                    <button
                      onClick={() => onOpenUri(n.obsidianUri)}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 text-[12px] font-medium text-zinc-300 opacity-80 transition hover:border-amber-400/40 hover:text-amber-200 group-hover:opacity-100"
                    >
                      Obsidian
                      <ExternalLink className="h-3 w-3" />
                    </button>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}
