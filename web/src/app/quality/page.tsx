"use client";

import { CheckCircle, WarningCircle, XCircle } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { PageShell } from "@/components/page-shell";
import { api, type QualityCheck, type QualityReport } from "@/lib/api";
import { cn } from "@/lib/utils";

const ICON = {
  pass: <CheckCircle size={15} weight="fill" className="text-primary" />,
  warn: <WarningCircle size={15} weight="fill" className="text-amber-500" />,
  fail: <XCircle size={15} weight="fill" className="text-destructive" />,
};
const STATUS_LABEL = { pass: "通过", warn: "警告", fail: "失败" };
const CATEGORIES = ["空值", "重复", "取值范围", "外键一致性", "业务规则", "新鲜度"];

function Stat({ value, label, tone }: { value: number; label: string; tone?: string }) {
  return (
    <div className="border-l-2 border-primary/60 pl-4">
      <div className={cn("font-mono text-3xl font-medium tabular-nums tracking-tight", tone)}>{value}</div>
      <div className="mt-1 text-sm">{label}</div>
    </div>
  );
}

export default function QualityPage() {
  const [dbs, setDbs] = useState<string[]>([]);
  const [db, setDb] = useState("shop_ops");
  const [asOf, setAsOf] = useState("");
  const [rep, setRep] = useState<QualityReport | null>(null);
  const [error, setError] = useState("");
  const [onlyIssues, setOnlyIssues] = useState(false);

  useEffect(() => {
    api.databases().then((d) => setDbs(d.map((x) => x.db_id))).catch(() => setDbs([]));
  }, []);
  useEffect(() => {
    setError("");
    setRep(null);
    api
      .quality(db, asOf || undefined)
      .then(setRep)
      .catch((e: Error) => setError(`加载失败：${e.message}`));
  }, [db, asOf]);

  const groups = useMemo(() => {
    const checks = (rep?.checks ?? []).filter((c) => !onlyIssues || c.status !== "pass");
    return CATEGORIES.map((cat) => [cat, checks.filter((c) => c.category === cat)] as [string, QualityCheck[]]).filter(
      ([, cs]) => cs.length > 0,
    );
  }, [rep, onlyIssues]);

  return (
    <PageShell
      title="数据质量"
      note="指标算得对的前提是数据本身没问题。主键、外键按库里声明的约束自动检查；空值、取值范围、业务规则、新鲜度按规则文件检查。问题行只给主键，不带其他列的取值。命令行：uv run python -m datapilot.quality <库名>。"
    >
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <select value={db} onChange={(e) => setDb(e.target.value)} className="rounded-md border bg-background px-2 py-1.5" aria-label="数据库">
          {(dbs.length ? dbs : [db]).map((d) => (
            <option key={d}>{d}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-muted-foreground">
          检查日期
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} className="rounded-md border bg-background px-2 py-1 text-foreground" />
        </label>
        <label className="flex items-center gap-2 text-muted-foreground">
          <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} />
          只看有问题的
        </label>
      </div>

      {error && <p className="text-sm text-muted-foreground">{error}</p>}

      {rep && (
        <>
          <div className="grid gap-8 sm:grid-cols-4">
            <Stat value={rep.summary.total} label={`检查项（${rep.as_of}）`} />
            <Stat value={rep.summary.pass} label="通过" />
            <Stat value={rep.summary.warn} label="警告" tone={rep.summary.warn ? "text-amber-500" : undefined} />
            <Stat value={rep.summary.fail} label="失败" tone={rep.summary.fail ? "text-destructive" : undefined} />
          </div>
          {groups.map(([cat, checks]) => (
            <section key={cat} className="space-y-3">
              <h2 className="text-lg font-medium tracking-tight">{cat}</h2>
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full text-sm">
                  <tbody className="divide-y">
                    {checks.map((c) => (
                      <tr key={c.id}>
                        <td className="w-24 whitespace-nowrap px-3 py-2">
                          <span className="inline-flex items-center gap-1.5">
                            {ICON[c.status]}
                            {STATUS_LABEL[c.status]}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-muted-foreground">{c.table}</td>
                        <td className="px-3 py-2">
                          {c.description}
                          {c.detail && <div className="text-xs text-muted-foreground">{c.detail}</div>}
                          {c.sample_keys.length > 0 && (
                            <div className="font-mono text-xs text-muted-foreground">例：{c.sample_keys.map(String).join(", ")}</div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs tabular-nums text-muted-foreground">
                          {c.failed ? `${c.failed} / ${c.total}` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
        </>
      )}
    </PageShell>
  );
}
