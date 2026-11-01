"use client";

import {
  ArrowClockwise,
  CaretRight,
  ChartBar,
  ChartLine,
  ChartPie,
  CheckCircle,
  CircleNotch,
  Lightning,
  Table,
  WarningCircle,
} from "@phosphor-icons/react";
import { useState } from "react";

import { ChartView } from "@/components/chart-view";
import { DataTable } from "@/components/data-table";
import { SqlBlock } from "@/components/sql-block";
import { Skeleton } from "@/components/ui/skeleton";
import type { ChartSpec, Done, Step, TableData } from "@/lib/api";
import { cn } from "@/lib/utils";

export type Turn = {
  id: string;
  question: string;
  status: "running" | "done" | "error" | "disconnected";
  steps: Step[];
  sql?: string;
  table?: TableData;
  chart?: ChartSpec;
  answer?: string;
  error?: string;
  done?: Done;
};

type View = "chart" | "table" | "sql";
const CHART_TYPES: { type: ChartSpec["type"]; label: string; icon: React.ReactNode }[] = [
  { type: "bar", label: "柱状图", icon: <ChartBar size={14} /> },
  { type: "line", label: "折线图", icon: <ChartLine size={14} /> },
  { type: "pie", label: "饼图", icon: <ChartPie size={14} /> },
];

function Steps({ turn }: { turn: Turn }) {
  const [open, setOpen] = useState(false);
  const running = turn.status === "running";
  const corrections = turn.steps.filter((s) => s.name === "correct").length;
  const summary = running
    ? `${turn.steps.at(-1)?.label ?? "理解问题"}…`
    : `${turn.steps.filter((s) => s.name !== "queue").length} 步${corrections ? `，修正 ${corrections} 次` : ""}`;

  if (!running && turn.done?.cached) {
    return <p className="text-sm text-muted-foreground">命中缓存，未调用模型</p>;
  }

  return (
    <div className="text-sm">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
        aria-expanded={open || running}
      >
        {running ? <CircleNotch size={14} className="animate-spin text-primary" /> : <CaretRight size={12} className={cn("transition-transform", open && "rotate-90")} />}
        <span>{running ? summary : `查询过程（${summary}）`}</span>
      </button>
      {(open || running) && turn.steps.length > 0 && (
        <ol className="mt-2 space-y-1.5 border-l pl-4">
          {turn.steps.map((s, i) => (
            <li key={i} className="flex items-start gap-2 text-muted-foreground">
              <CheckCircle size={14} className="mt-0.5 shrink-0 text-primary" weight="fill" />
              <div className="min-w-0">
                <span className="text-foreground/90">{s.label}</span>
                {s.issues && s.issues.length > 0 && (
                  <ul className="mt-0.5 space-y-0.5 text-xs">
                    {s.issues.map((iss, k) => (
                      <li key={k} className="break-words">{iss}</li>
                    ))}
                  </ul>
                )}
                {s.issues && s.issues.length === 0 && <span className="ml-2 text-xs">没有发现问题</span>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Kpi({ table }: { table: TableData }) {
  const v = table.rows[0]?.[0];
  return (
    <div className="py-4">
      <div className="text-xs text-muted-foreground">{table.columns[0]}</div>
      <div className="mt-1 font-mono text-4xl font-medium tabular-nums tracking-tight">
        {typeof v === "number" ? v.toLocaleString("zh-CN", { maximumFractionDigits: 4 }) : String(v ?? "NULL")}
      </div>
    </div>
  );
}

function Result({ turn }: { turn: Turn }) {
  const hasChart = !!turn.chart && turn.chart.type !== "table";
  // 不在首次渲染时定死：table 事件先于 chart 到达，那时还不知道有没有图
  const [picked, setView] = useState<View | null>(null);
  const view: View = picked ?? (hasChart ? "chart" : "table");
  const [chartType, setChartType] = useState<ChartSpec["type"] | null>(null);
  if (!turn.table || !turn.sql) return null;
  const chart = turn.chart && chartType ? { ...turn.chart, type: chartType } : turn.chart;
  const canSwitch = turn.chart && ["bar", "line", "pie"].includes(turn.chart.type);

  const tabs: { key: View; label: string; show: boolean }[] = [
    { key: "chart", label: turn.chart?.type === "kpi" ? "数值" : "图表", show: !!hasChart },
    { key: "table", label: "表格", show: true },
    { key: "sql", label: "SQL", show: true },
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex rounded-lg border p-0.5 text-sm">
          {tabs
            .filter((t) => t.show)
            .map((t) => (
              <button
                key={t.key}
                onClick={() => setView(t.key)}
                className={cn(
                  "rounded-md px-3 py-1 transition-colors",
                  view === t.key ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
              </button>
            ))}
        </div>
        {view === "chart" && canSwitch && (
          <div className="inline-flex gap-0.5 text-muted-foreground">
            {CHART_TYPES.map((c) => (
              <button
                key={c.type}
                title={c.label}
                aria-label={c.label}
                onClick={() => setChartType(c.type)}
                className={cn("rounded-md p-1.5 hover:bg-accent hover:text-foreground", chart?.type === c.type && "bg-accent text-foreground")}
              >
                {c.icon}
              </button>
            ))}
          </div>
        )}
      </div>
      {view === "chart" && chart && (chart.type === "kpi" ? <Kpi table={turn.table} /> : <ChartView spec={chart} data={turn.table} />)}
      {view === "table" && <DataTable data={turn.table} />}
      {view === "sql" && <SqlBlock sql={turn.sql} />}
    </div>
  );
}

export function TurnView({ turn, onRetry }: { turn: Turn; onRetry: () => void }) {
  return (
    <article className="space-y-4">
      <h2 className="text-[15px] font-medium leading-relaxed">{turn.question}</h2>
      <Steps turn={turn} />

      {turn.status === "running" && !turn.table && (
        <div className="space-y-2" aria-hidden>
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-56 w-full" />
        </div>
      )}

      {turn.answer && <p className="leading-relaxed text-foreground/95">{turn.answer}</p>}
      <Result turn={turn} />

      {(turn.status === "error" || turn.status === "disconnected") && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm">
          <WarningCircle size={16} className="mt-0.5 shrink-0 text-destructive" />
          <div className="flex-1 space-y-2">
            <p>{turn.status === "disconnected" ? "连接中断，没有收到完整结果。" : turn.error}</p>
            {turn.sql && turn.status === "error" && <SqlBlock sql={turn.sql} />}
          </div>
          <button onClick={onRetry} className="inline-flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground">
            <ArrowClockwise size={14} />
            重试
          </button>
        </div>
      )}

      {turn.done && turn.status === "done" && (
        <div className="flex items-center gap-3 font-mono text-xs text-muted-foreground tabular-nums">
          {turn.done.cached && (
            <span className="inline-flex items-center gap-1 text-primary">
              <Lightning size={12} weight="fill" />
              命中缓存
            </span>
          )}
          <span>{turn.done.latency_s.toFixed(1)} s</span>
          <span>${turn.done.cost_usd.toFixed(4)}</span>
          <span className="inline-flex items-center gap-1">
            <Table size={12} />
            {turn.table?.rows.length ?? 0} 行
          </span>
        </div>
      )}
    </article>
  );
}
