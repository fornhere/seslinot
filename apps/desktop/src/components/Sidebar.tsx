import {
  CheckSquare,
  Compass,
  FolderOpen,
  Mic,
  NotebookPen,
  Settings,
  Sparkles,
} from "lucide-react";
import type { AppSnapshot } from "../api";

export type NavTab = "studio" | "approvals" | "research" | "vault" | "settings";

interface SidebarProps {
  active: NavTab;
  onSelect: (tab: NavTab) => void;
  snapshot: AppSnapshot | null;
  onOpenVault: () => void;
}

export function Sidebar({ active, onSelect, snapshot, onOpenVault }: SidebarProps) {
  const approvalCount = snapshot?.desktop.pendingApproval.length ?? 0;
  const activeCount =
    (snapshot?.desktop.activeSessions.length ?? 0) +
    (snapshot?.desktop.pendingConfirm.length ?? 0);

  const items: {
    id: NavTab;
    label: string;
    icon: typeof Mic;
    badge?: number;
    badgeTone?: "amber" | "indigo";
  }[] = [
    { id: "studio", label: "Kayıt Stüdyosu", icon: Mic },
    {
      id: "approvals",
      label: "Onay Bekleyenler",
      icon: CheckSquare,
      badge: approvalCount,
      badgeTone: "amber",
    },
    {
      id: "research",
      label: "Derin Araştırma",
      icon: Compass,
      badge: activeCount,
      badgeTone: "indigo",
    },
    { id: "vault", label: "Kasa Akışı", icon: NotebookPen },
    { id: "settings", label: "Ayarlar", icon: Settings },
  ];

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-white/[0.06] bg-[#0c0e15]/90 px-3 py-4 select-none">
      {/* Marka başlığı */}
      <div className="mb-6 flex items-center gap-2.5 px-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-amber-400 via-amber-500 to-orange-600 shadow-lg shadow-amber-500/20">
          <Sparkles className="h-4.5 w-4.5 text-zinc-950" strokeWidth={2.4} />
        </div>
        <div>
          <div className="text-[15px] font-semibold tracking-tight text-white">SesliNot</div>
          <div className="text-[11px] font-medium text-zinc-500">
            v{snapshot?.version ?? "0.3.0"} · {snapshot?.platform ?? "desktop"}
          </div>
        </div>
      </div>

      {/* Gezinme */}
      <nav className="flex flex-1 flex-col gap-1">
        {items.map((item) => {
          const Icon = item.icon;
          const isCurrent = active === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={`group flex items-center justify-between rounded-xl px-3 py-2.5 text-left text-[13.5px] font-medium transition-all ${
                isCurrent
                  ? "bg-white/[0.08] text-white shadow-inner shadow-white/[0.04]"
                  : "text-zinc-400 hover:bg-white/[0.04] hover:text-zinc-200"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Icon
                  className={`h-4 w-4 transition-colors ${
                    isCurrent ? "text-amber-400" : "text-zinc-500 group-hover:text-zinc-300"
                  }`}
                />
                {item.label}
              </span>
              {item.badge && item.badge > 0 ? (
                <span
                  className={`rounded-full px-2 py-0.5 font-mono text-[11px] font-semibold ${
                    item.badgeTone === "amber"
                      ? "bg-amber-500/20 text-amber-300 ring-1 ring-amber-400/30"
                      : "bg-indigo-500/20 text-indigo-300 ring-1 ring-indigo-400/30"
                  }`}
                >
                  {item.badge}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      {/* Alt Kasa Durum Kartı */}
      <div className="mt-4 rounded-xl border border-white/[0.06] bg-white/[0.025] p-3">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            Obsidian Kasası
          </span>
          <span
            className={`h-2 w-2 rounded-full ${
              snapshot?.vault.exists ? "bg-emerald-400 shadow-sm shadow-emerald-400/60" : "bg-rose-400"
            }`}
          />
        </div>
        <div className="mt-1 truncate text-[13px] font-semibold text-zinc-200">
          {snapshot?.vault.name || "Seçilmedi"}
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[11px] text-zinc-500">
          <span>{snapshot?.vault.hafizaOs ? "Hafıza OS aktif" : "Standart Obsidian"}</span>
          <button
            onClick={onOpenVault}
            className="inline-flex items-center gap-1 text-zinc-400 transition hover:text-amber-300"
            title="Kasayı klasörde aç"
          >
            <FolderOpen className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </aside>
  );
}
