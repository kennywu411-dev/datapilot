"use client";

import { CaretRight, LockSimple, MagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { MetricDetail, MetricTypeBadge } from "@/components/metric-card";
import { PageShell } from "@/components/page-shell";
import { api, type Catalog, type MetricDef } from "@/lib/api";

const DEFAULT_DB = "shop_ops";

function MetricRow({ m, dims }: { m: MetricDef; dims: Record<string, string> }) {
  return (
    <details className="group rounded-lg border">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden">
        <CaretRight size={12} className="shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <span className="font-medium">{m.name}</span>
        <code className="font-mono text-xs text-muted-foreground">{m.id}</code>
        <MetricTypeBadge m={m} />
        {m.restricted.length > 0 && <LockSimple size={13} className="text-destructive" aria-label="当前角色无权使用" />}
        <span className="ml-auto hidden max-w-[50%] truncate text-sm text-muted-foreground md:block">{m.description}</span>
      </summary>
      <div className="border-t px-4 py-4">
        <MetricDetail m={m} dimensionNames={dims} />
      </div>
    </details>
  );
}

export default function MetricsPage() {
  const [dbs, setDbs] = useState<string[]>([]);
  const [db, setDb] = useState(DEFAULT_DB);
  const [roles, setRoles] = useState<{ role: string; label: string }[]>([]);
  const [role, setRole] = useState("");
  const [cat, setCat] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    api.databases().then((d) => setDbs(d.map((x) => x.db_id))).catch(() => setDbs([]));
  }, []);
  useEffect(() => {
    api.roles(db).then(setRoles).catch(() => setRoles([]));
  }, [db]);
  useEffect(() => {
    // 切换库或角色时旧请求可能晚到：只采用最后一次请求的结果
    let stale = false;
    setError("");
    api
      .metrics(db, role || undefined)
      .then((c) => !stale && setCat(c))
      .catch((e: Error) => {
        if (stale) return;
        setCat(null);
        setError(e.message.startsWith("404") ? `${db} 还没有指标字典` : `加载失败：${e.message}`);
      });
    return () => {
      stale = true;
    };
  }, [db, role]);

  const dims = useMemo(() => Object.fromEntries((cat?.dimensions ?? []).map((d) => [d.id, d.name])), [cat]);
  const hit = (m: MetricDef) => {
    const q = query.trim().toLowerCase();
    return !q || [m.name, m.id, m.description, ...m.synonyms].some((s) => s.toLowerCase().includes(q));
  };
  const metrics = (cat?.metrics ?? []).filter(hit);
  const terms = (cat?.terms ?? []).filter(hit);

  return (
    <PageShell
      title="指标目录"
      note="公司口径的唯一出处：问数时先按同义词匹配这里的指标和术语，把口径交给生成 SQL 的模型。每个指标列出计算方式、过滤条件、可分析维度，以及依赖的表和列（血缘）；切换角色可以看到哪些指标因列权限不能使用。"
    >
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <select
          value={db}
          onChange={(e) => {
            setDb(e.target.value);
            setRole(""); // 角色是按库定义的，换库时一并重置，避免带着旧角色多发一次请求
          }}
          className="rounded-md border bg-background px-2 py-1.5" aria-label="数据库">
          {(dbs.length ? dbs : [DEFAULT_DB]).map((d) => (
            <option key={d}>{d}</option>
          ))}
        </select>
        {roles.length > 0 && (
          <select value={role} onChange={(e) => setRole(e.target.value)} className="rounded-md border bg-background px-2 py-1.5" aria-label="角色">
            <option value="">不限角色</option>
            {roles.map((r) => (
              <option key={r.role} value={r.role}>
                {r.label}
              </option>
            ))}
          </select>
        )}
        <label className="flex min-w-56 flex-1 items-center gap-2 rounded-md border px-2 py-1.5">
          <MagnifyingGlass size={14} className="text-muted-foreground" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索指标名称、同义词，如 GMV、复购" className="w-full bg-transparent outline-none" />
        </label>
      </div>

      {error && <p className="text-sm text-muted-foreground">{error}</p>}

      {cat && (
        <>
          <section className="space-y-3">
            <h2 className="text-lg font-medium tracking-tight">
              指标 <span className="text-sm font-normal text-muted-foreground">{metrics.length} / {cat.metrics.length}</span>
            </h2>
            <div className="space-y-2">
              {metrics.map((m) => (
                <MetricRow key={m.id} m={m} dims={dims} />
              ))}
            </div>
          </section>

          <section className="space-y-3">
            <h2 className="text-lg font-medium tracking-tight">
              业务术语 <span className="text-sm font-normal text-muted-foreground">{terms.length} / {cat.terms.length}</span>
            </h2>
            <div className="space-y-2">
              {terms.map((m) => (
                <MetricRow key={m.id} m={m} dims={dims} />
              ))}
            </div>
          </section>

          <section className="space-y-3">
            <h2 className="text-lg font-medium tracking-tight">维度</h2>
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-surface text-muted-foreground">
                  <tr>
                    {["维度", "表达式", "取值"].map((h) => (
                      <th key={h} className="whitespace-nowrap px-3 py-2 text-left font-medium">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {cat.dimensions.map((d) => (
                    <tr key={d.id}>
                      <td className="whitespace-nowrap px-3 py-2">
                        {d.name} <code className="ml-1 font-mono text-xs text-muted-foreground">{d.id}</code>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs">{d.expr.replace("{time}", "<时间列>")}</td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {Object.entries(d.values)
                          .map(([k, v]) => `${k}=${v}`)
                          .join("，") || d.description}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="grid gap-8 md:grid-cols-2">
            <div className="space-y-3">
              <h2 className="text-lg font-medium tracking-tight">通用约定</h2>
              <ul className="list-disc space-y-1.5 pl-5 text-sm leading-relaxed">
                {cat.conventions.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </div>
            <div className="space-y-3">
              <h2 className="text-lg font-medium tracking-tight">表关联</h2>
              <p className="text-xs text-muted-foreground">只沿子表 → 父表（多对一）补 JOIN，避免一对多把指标算重。</p>
              <ul className="space-y-1 font-mono text-xs">
                {cat.joins.map((j) => (
                  <li key={j.on}>
                    {j.left} → {j.right}：{j.on}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        </>
      )}
    </PageShell>
  );
}
