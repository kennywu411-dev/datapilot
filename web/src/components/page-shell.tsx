"use client";

import { Moon, Sun } from "@phosphor-icons/react";

import { TopNav } from "@/components/top-nav";
import { toggleTheme, useIsDark } from "@/lib/theme";

/** 指标目录、数据质量等独立页面的外框：顶栏 + 标题 + 说明。 */
export function PageShell({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  const dark = useIsDark();
  return (
    <div className="min-h-[100dvh]">
      <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b bg-background/90 px-4 backdrop-blur md:px-8">
        <span className="font-medium tracking-tight">DataPilot</span>
        <div className="flex items-center gap-2">
          <TopNav />
          <button onClick={toggleTheme} aria-label="切换深浅色" className="rounded-md p-2 text-muted-foreground hover:bg-accent hover:text-foreground">
            {dark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-10 px-4 py-10 md:px-8">
        <div>
          <h1 className="text-2xl font-medium tracking-tight">{title}</h1>
          {note && <p className="mt-2 max-w-[70ch] text-sm leading-relaxed text-muted-foreground">{note}</p>}
        </div>
        {children}
      </main>
    </div>
  );
}
