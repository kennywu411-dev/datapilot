"use client";

import { CaretRight, Database, Key, LinkSimple, Lock, Moon, Sun } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, type TableInfo } from "@/lib/api";
import { toggleTheme, useIsDark } from "@/lib/theme";
import { cn } from "@/lib/utils";

function TableItem({ t }: { t: TableInfo }) {
  const [open, setOpen] = useState(false);
  const fks = new Map(t.foreign_keys.map((f) => [f.column, `${f.ref_table}.${f.ref_column}`]));
  return (
    <li>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-sm hover:bg-sidebar-accent"
        aria-expanded={open}
      >
        <CaretRight size={11} className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        <span className="truncate">{t.table}</span>
        <span className="ml-auto font-mono text-xs text-muted-foreground">{t.columns.length}</span>
      </button>
      {open && (
        <ul className="mb-1 ml-5 border-l pl-2">
          {t.columns.map((c) => {
            if (c.restricted)
              return (
                <li key={c.name} className="flex items-center gap-1.5 py-0.5 text-xs text-muted-foreground/60">
                  <span className="truncate line-through">{c.name}</span>
                  <Lock size={11} className="shrink-0" />
                  <span className="ml-auto shrink-0 text-[11px]">无权限</span>
                </li>
              );
            const desc = [c.description, c.value_description].filter(Boolean).join("\n");
            const row = (
              <li className="flex items-center gap-1.5 py-0.5 text-xs">
                <span className="truncate text-foreground/85">{c.name}</span>
                {c.primary_key && <Key size={11} className="shrink-0 text-primary" />}
                {fks.has(c.name) && <LinkSimple size={11} className="shrink-0 text-muted-foreground" />}
                <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">{(c.type || "").toLowerCase()}</span>
              </li>
            );
            return desc || fks.has(c.name) ? (
              <Tooltip key={c.name}>
                <TooltipTrigger render={row} />
                <TooltipContent side="right" className="max-w-xs whitespace-pre-line">
                  {fks.has(c.name) ? `外键 → ${fks.get(c.name)}\n` : ""}
                  {desc}
                </TooltipContent>
              </Tooltip>
            ) : (
              <li key={c.name} className="flex items-center gap-1.5 py-0.5 text-xs">
                {row.props.children}
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

export function Sidebar({
  dbs,
  db,
  onDbChange,
  health,
  roles = [],
  role = "",
  onRoleChange,
}: {
  dbs: { db_id: string; tables: number }[];
  db: string;
  onDbChange: (db: string) => void;
  health: { store: string; model: string } | null;
  roles?: { role: string; label: string }[];
  role?: string;
  onRoleChange?: (role: string) => void;
}) {
  const [tables, setTables] = useState<TableInfo[] | null>(null);
  const dark = useIsDark();

  useEffect(() => {
    if (!db) return;
    setTables(null);
    api.schema(db, role || undefined).then((s) => setTables(s.tables)).catch(() => setTables([]));
  }, [db, role]);

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
      <div className="flex h-14 items-center gap-2 px-4">
        <Database size={18} weight="duotone" className="text-primary" />
        <span className="font-medium tracking-tight">DataPilot</span>
      </div>

      <div className="px-3 pb-3">
        <label htmlFor="db" className="mb-1.5 block px-1 text-xs text-muted-foreground">
          数据库
        </label>
        <select
          id="db"
          value={db}
          onChange={(e) => onDbChange(e.target.value)}
          className="h-9 w-full rounded-md border bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {dbs.map((d) => (
            <option key={d.db_id} value={d.db_id}>
              {d.db_id}
            </option>
          ))}
        </select>
        {roles.length > 0 && (
          <>
            <label htmlFor="role" className="mb-1.5 mt-3 block px-1 text-xs text-muted-foreground">
              角色（行列级权限演示）
            </label>
            <select
              id="role"
              value={role}
              onChange={(e) => onRoleChange?.(e.target.value)}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {roles.map((r) => (
                <option key={r.role} value={r.role}>
                  {r.label}
                </option>
              ))}
            </select>
          </>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        <div className="px-2 pb-1.5 text-xs text-muted-foreground">表结构</div>
        {tables === null ? (
          <div className="space-y-2 px-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-5 w-full" />
            ))}
          </div>
        ) : (
          <ul>
            {tables.map((t) => (
              <TableItem key={t.table} t={t} />
            ))}
          </ul>
        )}
      </div>

      <div className="flex items-center justify-between border-t px-4 py-3 text-xs text-muted-foreground">
        <span className="font-mono">
          {health ? `${health.model} · ${health.store === "redis" ? "Redis" : "内存存储"}` : "后端未连接"}
        </span>
        <button onClick={toggleTheme} aria-label="切换深浅色" className="rounded-md p-1.5 hover:bg-sidebar-accent hover:text-foreground">
          {dark ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
    </aside>
  );
}
