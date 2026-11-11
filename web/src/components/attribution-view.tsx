"use client";

import { CaretRight, CheckCircle, WarningCircle } from "@phosphor-icons/react";
import { BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useMemo, useRef, useState } from "react";

import { SqlBlock } from "@/components/sql-block";
import type { AttrBreakdown, AttrCause, AttrEvidence, Attribution, NumberCheck } from "@/lib/api";
import { CHART_THEME, useIsDark } from "@/lib/theme";
import { cn } from "@/lib/utils";

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

type Metric = Attribution["metric"];

/** 和后端 Formatter 同样的写法：回答里的数字和这里显示的是同一个字符串。 */
function fmtValue(m: Metric, v: number) {
  if (m.unit === "percent") return `${(v * 100).toFixed(2)}%`;
  if (m.unit === "money") return `${m.currency}${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return Number.isInteger(v) ? v.toLocaleString("en-US") : v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function fmtChange(m: Metric, v: number) {
  const sign = v >= 0 ? "+" : "−";
  const a = Math.abs(v);
  if (m.unit === "percent") return `${sign}${(a * 100).toFixed(2)} 个百分点`;
  if (m.unit === "money") return `${sign}${m.currency}${a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return `${sign}${a.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}
const pct = (v: number) => `${(v * 100).toFixed(1)}%`;
const dayBefore = (d: string) => {
  const t = new Date(`${d}T00:00:00Z`);
  t.setUTCDate(t.getUTCDate() - 1);
  return t.toISOString().slice(0, 10);
};
const period = (p: string[]) => `${p[0]} 至 ${dayBefore(p[1])}`;

/** 回答下面的数字核对结果：每个数字都要能在 SQL 结果或事实表里找到。 */
export function NumberCheckBadge({ check, source }: { check: NumberCheck; source?: string }) {
  const ok = check.unmatched.length === 0;
  return (
    <p className={cn("flex flex-wrap items-center gap-x-2 gap-y-1 text-xs", ok ? "text-muted-foreground" : "text-destructive")}>
      {ok ? <CheckCircle size={14} weight="fill" className="text-primary" /> : <WarningCircle size={14} />}
      <span>
        数字核对：回答里 {check.total} 个数字{ok ? "全部对上" : `有 ${check.unmatched.length} 个对不上（${check.unmatched.join("、")}）`}
        ，其中 {check.from_sql} 个直接出自 SQL 结果，{check.derived} 个由代码按公式从 SQL 结果算出
      </span>
      {source === "rewrite" && <span className="rounded border px-1.5">第一次没通过，已重写</span>}
      {source === "template" && <span className="rounded border px-1.5">两次没通过，改用模板生成</span>}
    </p>
  );
}

function ContributionChart({ b }: { b: AttrBreakdown }) {
  const ref = useRef<HTMLDivElement>(null);
  const dark = useIsDark();
  const rows = useMemo(() => [...b.segments].sort((x, y) => Math.abs(y.contribution) - Math.abs(x.contribution)).slice(0, 10), [b]);

  useEffect(() => {
    if (!ref.current) return;
    const t = dark ? CHART_THEME.dark : CHART_THEME.light;
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    const view = [...rows].reverse(); // ECharts 的类目轴从下往上画
    chart.setOption({
      grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 12 },
        valueFormatter: (v: unknown) => `${Number(v).toFixed(1)}%`,
      },
      xAxis: { type: "value", axisLabel: { color: t.text, formatter: "{value}%" }, splitLine: { lineStyle: { color: t.split } } },
      yAxis: {
        type: "category",
        data: view.map((s) => s.label),
        axisLine: { lineStyle: { color: t.axis } },
        axisTick: { show: false },
        axisLabel: { color: t.text, width: 120, overflow: "truncate" },
      },
      series: [
        {
          name: "占总变化",
          type: "bar",
          barMaxWidth: 16,
          data: view.map((s) => ({
            value: +(s.contribution * 100).toFixed(1),
            // 方向和总变化一致的用主色，对冲的用灰色
            itemStyle: { color: s.contribution >= 0 ? t.accent : t.series[1], borderRadius: 3 },
          })),
          label: { show: true, position: "right", color: t.text, fontSize: 11, formatter: "{c}%" },
        },
        {
          name: "预期份额",
          type: "bar",
          barMaxWidth: 6,
          barGap: "-70%",
          z: 3,
          data: view.map((s) => +(s.expected * 100).toFixed(1)),
          itemStyle: { color: t.text, opacity: 0.55, borderRadius: 2 },
        },
      ],
    });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(ref.current);
    return () => {
      resize.disconnect();
      chart.dispose();
    };
  }, [rows, dark]);

  return <div ref={ref} style={{ height: Math.max(120, rows.length * 30 + 24) }} className="w-full" />;
}

function BreakdownTable({ b, m }: { b: AttrBreakdown; m: Metric }) {
  const rows = b.segments.slice(0, 12);
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-surface text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left font-medium">{b.name}</th>
            <th className="px-3 py-2 text-right font-medium">基期</th>
            <th className="px-3 py-2 text-right font-medium">本期</th>
            <th className="px-3 py-2 text-right font-medium">变化</th>
            <th className="px-3 py-2 text-right font-medium">占总变化</th>
            <th className="px-3 py-2 text-right font-medium">预期份额</th>
            <th className="px-3 py-2 text-right font-medium">正常波动</th>
            <th className="px-3 py-2 text-right font-medium">得分</th>
          </tr>
        </thead>
        <tbody className="divide-y font-mono tabular-nums">
          {rows.map((s, i) => (
            <tr key={i} className={cn("hover:bg-accent/40", s.score >= 0.2 && s.contribution > 0 && "bg-accent/30")}>
              <td className="max-w-[16rem] truncate px-3 py-1.5 font-sans">{s.label}</td>
              <td className="px-3 py-1.5 text-right">{b.kind === "document" && s.period !== "base" ? "—" : fmtValue(m, s.base)}</td>
              <td className="px-3 py-1.5 text-right">{b.kind === "document" && s.period !== "current" ? "—" : fmtValue(m, s.current)}</td>
              <td className="px-3 py-1.5 text-right">{fmtChange(m, s.change)}</td>
              <td className="px-3 py-1.5 text-right">{pct(s.contribution)}</td>
              <td className="px-3 py-1.5 text-right">{pct(s.expected)}</td>
              <td className="px-3 py-1.5 text-right">{s.noise ? `±${pct(s.noise)}` : "—"}</td>
              <td className="px-3 py-1.5 text-right">{pct(s.score)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceList({ ids, all }: { ids: string[]; all: AttrEvidence[] }) {
  const [open, setOpen] = useState(false);
  const list = all.filter((e) => ids.includes(e.id));
  if (list.length === 0) return null;
  return (
    <div className="text-sm">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
      >
        <CaretRight size={11} className={cn("transition-transform", open && "rotate-90")} />
        对应的 SQL（{list.map((e) => e.id).join("、")}）
      </button>
      {open && (
        <div className="mt-2 space-y-3">
          {list.map((e) => (
            <div key={e.id} className="space-y-1.5">
              <p className="text-xs text-muted-foreground">
                <span className="font-mono text-foreground/80">{e.id}</span> {e.purpose} · {(e.elapsed_s * 1000).toFixed(0)} ms · {e.rows.length} 行
              </p>
              <SqlBlock sql={e.sql} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CauseCard({ c, i, m, all }: { c: AttrCause; i: number; m: Metric; all: AttrEvidence[] }) {
  const detail = Object.entries(c.detail);
  return (
    <li className="space-y-2 rounded-lg border px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-xs text-muted-foreground">原因 {i + 1}</span>
        <span className="font-medium">{c.label}</span>
        <span className="font-mono text-sm tabular-nums text-primary">占总变化 {pct(c.contribution)}</span>
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        贡献项变化 {fmtChange(m, c.change)}；预期份额 {pct(c.expected)}
        {c.noise ? `；正常波动 ±${pct(c.noise)}` : ""}
        {c.note ? `；${c.note}` : ""}
      </p>
      {detail.length > 0 && (
        <p className="font-mono text-xs text-muted-foreground">{detail.map(([k, v]) => `${k} = ${String(v)}`).join(" · ")}</p>
      )}
      <EvidenceList ids={c.evidence} all={all} />
    </li>
  );
}

function Breakdowns({ list, m, all }: { list: AttrBreakdown[]; m: Metric; all: AttrEvidence[] }) {
  const [tab, setTab] = useState(0);
  const b = list[Math.min(tab, list.length - 1)];
  if (!b) return null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1 text-sm">
        {list.map((x, i) => (
          <button
            key={x.dimension}
            onClick={() => setTab(i)}
            className={cn(
              "rounded-md border px-2.5 py-1 transition-colors",
              i === tab ? "border-primary/40 bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {x.name}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {b.kind === "document"
          ? "两个时间段各自金额最大的几张单据。预期份额取另一个时间段同名次的单据：两期都有一张差不多大的，就互相抵消。"
          : b.kind === "entity"
            ? "取值很多，只列出变化最大的若干个。粗条是它占总变化的比例，细条是按基期份额该分到的比例。"
            : "粗条是这个分组占总变化的比例（负数表示方向相反、在对冲），细条是按基期份额该分到的比例。两者之差扣掉正常波动才是得分。"}
      </p>
      <ContributionChart b={b} />
      <BreakdownTable b={b} m={m} />
      <EvidenceList ids={b.evidence} all={all} />
    </div>
  );
}

export function AttributionView({ data }: { data: Attribution }) {
  const m = data.metric;
  const t = data.total;
  const [showAll, setShowAll] = useState(false);
  return (
    <section className="space-y-6">
      <div className="rounded-lg border px-4 py-3">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
          <div>
            <div className="text-xs text-muted-foreground">基期 {period(data.scope.base)}</div>
            <div className="mt-0.5 font-mono text-xl tabular-nums">{fmtValue(m, t.base)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">本期 {period(data.scope.current)}</div>
            <div className="mt-0.5 font-mono text-xl tabular-nums">{fmtValue(m, t.current)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{m.name}变化</div>
            <div className="mt-0.5 font-mono text-xl tabular-nums text-primary">
              {fmtChange(m, t.change)}
              {t.pct_change !== null && m.unit !== "percent" && (
                <span className="ml-2 text-sm text-muted-foreground">
                  {t.pct_change >= 0 ? "+" : "−"}
                  {pct(Math.abs(t.pct_change))}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="mt-3">
          <EvidenceList ids={t.evidence} all={data.evidence} />
        </div>
        {data.role_note && <p className="mt-2 text-xs text-muted-foreground">当前角色只能看到部分数据，以上只代表有权限的范围。</p>}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">原因（按重要性）</h3>
        {data.causes.length > 0 ? (
          <ol className="space-y-2">
            {data.causes.map((c, i) => (
              <CauseCard key={i} c={c} i={i} m={m} all={data.evidence} />
            ))}
          </ol>
        ) : (
          <p className="text-sm text-muted-foreground">没有找到突出的原因。</p>
        )}
        {data.rounds.map((r, i) => (
          <p key={i} className="text-xs text-muted-foreground">
            去掉 {r.removed} 之后，剩余变化 <span className="font-mono">{fmtChange(m, r.change)}</span>
          </p>
        ))}
        {data.stop_reason && <p className="text-xs text-muted-foreground">停止下钻的理由：{data.stop_reason}。</p>}
      </div>

      {data.breakdowns.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">第一层：按各维度分解</h3>
          <Breakdowns list={data.breakdowns} m={m} all={data.evidence} />
        </div>
      )}

      {data.drill.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">第二层：在 {data.drill[0].within.map((w) => `${w.name}=${w.label}`).join(" × ")} 之内再拆</h3>
          <Breakdowns list={data.drill} m={m} all={data.evidence} />
        </div>
      )}

      <div className="space-y-2">
        <button
          onClick={() => setShowAll((o) => !o)}
          aria-expanded={showAll}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <CaretRight size={12} className={cn("transition-transform", showAll && "rotate-90")} />
          这次归因执行的全部 SQL（{data.evidence.length} 条，只读）
        </button>
        {showAll && (
          <ol className="space-y-3">
            {data.evidence.map((e) => (
              <li key={e.id} className="space-y-1.5">
                <p className="text-xs text-muted-foreground">
                  <span className="font-mono text-foreground/80">{e.id}</span> {e.purpose} · {(e.elapsed_s * 1000).toFixed(0)} ms · {e.rows.length} 行
                </p>
                <SqlBlock sql={e.sql} />
              </li>
            ))}
          </ol>
        )}
        {data.skipped.length > 0 && <p className="text-xs text-muted-foreground">没有用上的维度：{data.skipped.join("；")}</p>}
      </div>
    </section>
  );
}
