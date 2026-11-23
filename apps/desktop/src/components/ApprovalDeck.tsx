import { useEffect, useMemo, useState } from "react";
import { marked } from "marked";
import {
  Check,
  ExternalLink,
  FileCheck2,
  Loader2,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import type { PendingApproval, ReportPreview, SesliNotApi } from "../api";

interface ApprovalDeckProps {
  api: SesliNotApi;
  items: PendingApproval[];
  onApprove: (jobId: string) => Promise<void>;
  onReject: (jobId: string) => Promise<void>;
  onOpenExternal: (path: string) => void;
}

function renderSafeMarkdown(raw: string): string {
  // HTML etiketlerini metinleştirip yalnız güvenli Markdown HTML'i üretir
  const escaped = raw
    .replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, "")
    .replace(/<iframe[\s\S]*?>[\s\S]*?<\/iframe>/gi, "")
    .replace(/ on\w+="[^"]*"/gi, "");
  return marked.parse(escaped, { async: false, gfm: true, breaks: true }) as string;
}

export function ApprovalDeck({
  api,
  items,
  onApprove,
  onReject,
  onOpenExternal,
}: ApprovalDeckProps) {
  const [selectedId, setSelectedId] = useState<string>(items[0]?.id ?? "");
  const [preview, setPreview] = useState<ReportPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<"approve" | "reject" | null>(null);

  useEffect(() => {
    if (!items.length) {
      setSelectedId("");
      setPreview(null);
      return;
    }
    if (!items.some((i) => i.id === selectedId)) {
      setSelectedId(items[0].id);
    }
  }, [items, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    setLoading(true);
    api
      .getJobPreview(selectedId)
      .then((res) => {
        if (!cancelled) setPreview(res);
      })
      .catch(() => {
        if (!cancelled) setPreview(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, selectedId]);

  const activeItem = items.find((i) => i.id === selectedId) ?? items[0] ?? null;
  const renderedHtml = useMemo(
    () => (preview?.markdown ? renderSafeMarkdown(preview.markdown) : ""),
    [preview?.markdown]
  );

  if (!items.length) {
    return (
      <div className="glass-card flex flex-col items-center justify-center rounded-2xl px-8 py-20 text-center">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-400 ring-1 ring-emerald-400/20">
          <FileCheck2 className="h-7 w-7" />
        </div>
        <h2 className="text-lg font-semibold text-white">Kayıt onayı bekleyen rapor yok</h2>
        <p className="mt-2 max-w-md text-[13.5px] leading-relaxed text-zinc-400">
          Derin bir araştırma tamamlandığında kasanı doğrudan kirletmez; önce burada tam Markdown
          önizlemesi, doğrulanmış kaynak sayısı ve denetim sonucu ile onayına sunulur.
        </p>
      </div>
    );
  }

  return (
    <div className="grid h-[calc(100vh-110px)] grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Sol liste */}
      <div className="flex flex-col gap-2.5 overflow-y-auto pr-1 lg:col-span-4">
        <div className="px-1 text-[11px] font-semibold uppercase tracking-wider text-amber-300/90">
          Kayıt Onayı Bekleyenler ({items.length})
        </div>
        {items.map((item) => {
          const selected = item.id === activeItem?.id;
          return (
            <button
              key={item.id}
              onClick={() => setSelectedId(item.id)}
              className={`glass-card rounded-xl p-4 text-left transition-all ${
                selected
                  ? "border-amber-400/45 bg-amber-500/[0.06] ring-1 ring-amber-400/30"
                  : "hover:border-white/15"
              }`}
            >
              <div className="line-clamp-2 text-[14px] font-semibold text-white">{item.title}</div>
              {item.summary ? (
                <p className="mt-1.5 line-clamp-2 text-[12.5px] leading-relaxed text-zinc-400">
                  {item.summary}
                </p>
              ) : null}
              <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px] font-mono">
                <span className="rounded-md bg-emerald-500/15 px-2 py-0.5 text-emerald-300">
                  {item.sourceCount} kaynak
                </span>
                <span className="rounded-md bg-sky-500/15 px-2 py-0.5 text-sky-300">
                  {item.verifiedQuotes} alıntı
                </span>
                <span className="rounded-md bg-amber-500/15 px-2 py-0.5 text-amber-300">
                  güven: {item.confidence}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      {/* Sağ tam Markdown önizleme + aksiyon çubuğu */}
      <div className="glass-card flex flex-col overflow-hidden rounded-2xl lg:col-span-8">
        {activeItem ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.07] bg-black/25 px-5 py-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-[11.5px] font-medium text-amber-300">
                  <ShieldCheck className="h-3.5 w-3.5" />
                  <span>
                    Denetim: {activeItem.auditStatus} · {activeItem.sourceCount} doğrulanmış kaynak ·{" "}
                    {activeItem.verifiedQuotes} birebir/yakın alıntı
                  </span>
                </div>
                <h2 className="mt-0.5 truncate text-base font-semibold text-white">
                  {activeItem.title}
                </h2>
              </div>

              <div className="flex items-center gap-2">
                {activeItem.previewPath ? (
                  <button
                    onClick={() => onOpenExternal(activeItem.previewPath)}
                    className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2 text-[12.5px] font-medium text-zinc-300 transition hover:bg-white/[0.09]"
                    title="Varsayılan Markdown düzenleyicide aç"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    Dosya
                  </button>
                ) : null}

                <button
                  disabled={acting !== null}
                  onClick={async () => {
                    setActing("reject");
                    try {
                      await onReject(activeItem.id);
                    } finally {
                      setActing(null);
                    }
                  }}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-rose-500/25 bg-rose-500/10 px-3.5 py-2 text-[12.5px] font-semibold text-rose-300 transition hover:bg-rose-500/20 disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Reddet
                </button>

                <button
                  disabled={acting !== null}
                  onClick={async () => {
                    setActing("approve");
                    try {
                      await onApprove(activeItem.id);
                    } finally {
                      setActing(null);
                    }
                  }}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 px-4 py-2 text-[13px] font-semibold text-zinc-950 shadow-lg shadow-emerald-500/20 transition hover:brightness-110 disabled:opacity-50"
                >
                  {acting === "approve" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Check className="h-4 w-4" strokeWidth={2.6} />
                  )}
                  Kasaya Kaydet
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {loading ? (
                <div className="flex h-48 items-center justify-center gap-2 text-zinc-400">
                  <Loader2 className="h-5 w-5 animate-spin text-amber-400" />
                  <span className="text-sm">Rapor önizlemesi yükleniyor...</span>
                </div>
              ) : renderedHtml ? (
                <article
                  className="md-preview"
                  dangerouslySetInnerHTML={{ __html: renderedHtml }}
                />
              ) : (
                <div className="flex flex-col items-center justify-center py-16 text-zinc-500">
                  <Sparkles className="mb-2 h-6 w-6" />
                  Önizleme içeriği bulunamadı.
                </div>
              )}
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
