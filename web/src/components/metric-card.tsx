"use client";

import { LockSimple } from "@phosphor-icons/react";

import { SqlBlock } from "@/components/sql-block";
import type { MetricDef } from "@/lib/api";

const TYPE_LABEL: Record<MetricDef["type"], string> = {
  simple: "可直接生成 SQL",
  derived: "复合指标",
  term: "业务术语",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[6.5rem_1fr] sm:gap-3">
      <dt className="text-xs text-muted-foreground sm:pt-0.5">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}

function Chips({ items, mono }: { items: string[]; mono?: boolean }) {
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((x) => (
        <span key={x} className={`rounded border px-1.5 py-0.5 text-xs ${mono ? "font-mono" : ""}`}>
          {x}
        </span>
      ))}
    </div>
  );
}

/** 一个指标 / 术语的口径：说明、计算、条件、参考 SQL、同义词、维度、血缘。 */
export function MetricDetail({ m, dimensionNames }: { m: MetricDef; dimensionNames?: Record<string, string> }) {
  return (
    <dl className="space-y-2.5">
      <Field label="口径">{m.description}</Field>
      {m.expr && (
        <Field label="计算">
          <code className="font-mono text-xs">{m.expr}</code>
          {m.base && <span className="ml-2 text-xs text-muted-foreground">主表 {m.base}</span>}
        </Field>
      )}
      {(m.filters?.length ?? 0) > 0 && (
        <Field label="过滤条件">
          <div className="space-y-0.5">
            {m.filters!.map((f, i) => (
              <div key={i} className="text-xs">
                <span className="text-muted-foreground">{f.name}：</span>
                <code className="font-mono">{f.sql}</code>
              </div>
            ))}
          </div>
        </Field>
      )}
      {m.sql && (
        <Field label="条件">
          <code className="font-mono text-xs">{m.sql}</code>
        </Field>
      )}
      {m.example_sql && (
        <Field label="整体 SQL">
          <SqlBlock sql={m.example_sql} />
        </Field>
      )}
      {m.reference_sql && (
        <Field label="参考 SQL">
          <SqlBlock sql={m.reference_sql} />
        </Field>
      )}
      {m.synonyms.length > 0 && (
        <Field label="同义词">
          <Chips items={m.synonyms} />
        </Field>
      )}
      {(m.dimensions?.length ?? 0) > 0 && (
        <Field label="可分析维度">
          <Chips items={m.dimensions!.map((d, i) => dimensionNames?.[d] ?? m.dimension_names?.[i] ?? d)} />
        </Field>
      )}
      <Field label="血缘">
        <Chips items={m.lineage} mono />
      </Field>
      {m.restricted.length > 0 && (
        <Field label="权限">
          <span className="inline-flex items-center gap-1 text-xs text-destructive">
            <LockSimple size={12} />
            当前角色无权访问 {m.restricted.join("、")}，不能使用这个指标
          </span>
        </Field>
      )}
    </dl>
  );
}

export function MetricTypeBadge({ m }: { m: MetricDef }) {
  return <span className="rounded bg-accent px-1.5 py-0.5 text-[11px] text-muted-foreground">{TYPE_LABEL[m.type]}</span>;
}
