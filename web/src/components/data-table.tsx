"use client";

import { CaretDown, CaretUp } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import type { TableData } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

function isNum(v: unknown) {
  return typeof v === "number";
}

function fmt(v: unknown) {
  if (v === null || v === undefined) return <span className="text-muted-foreground/60">NULL</span>;
  if (v === "") return <span className="text-muted-foreground/60">(空)</span>;
  if (typeof v === "number" && !Number.isInteger(v)) return v.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
  return String(v);
}

export function DataTable({ data }: { data: TableData }) {
  const [sort, setSort] = useState<{ col: number; asc: boolean } | null>(null);
  const [page, setPage] = useState(0);

  const rows = useMemo(() => {
    if (!sort) return data.rows;
    return [...data.rows].sort((a, b) => {
      const x = a[sort.col];
      const y = b[sort.col];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      const r = isNum(x) && isNum(y) ? (x as number) - (y as number) : String(x).localeCompare(String(y), "zh-CN");
      return sort.asc ? r : -r;
    });
  }, [data.rows, sort]);

  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const view = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const numericCol = data.columns.map((_, i) => data.rows.length > 0 && data.rows.every((r) => r[i] === null || isNum(r[i])));

  if (data.rows.length === 0) {
    return <p className="px-1 py-6 text-sm text-muted-foreground">查询成功，但没有返回任何行。</p>;
  }

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-surface">
            <tr>
              {data.columns.map((c, i) => (
                <th key={c + i} className={cn("px-3 py-2 font-medium text-muted-foreground", numericCol[i] ? "text-right" : "text-left")}>
                  <button
                    className="inline-flex items-center gap-1 whitespace-nowrap hover:text-foreground"
                    onClick={() => {
                      setPage(0);
                      setSort((s) => (s?.col === i ? (s.asc ? { col: i, asc: false } : null) : { col: i, asc: true }));
                    }}
                  >
                    {c}
                    {sort?.col === i && (sort.asc ? <CaretUp size={12} /> : <CaretDown size={12} />)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {view.map((r, ri) => (
              <tr key={ri} className="hover:bg-accent/40">
                {r.map((v, ci) => (
                  <td key={ci} className={cn("max-w-[28rem] truncate px-3 py-1.5", numericCol[ci] && "text-right font-mono tabular-nums")}>
                    {fmt(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          共 {data.rows.length} 行{data.truncated && "（结果较多，只显示前 500 行）"}
        </span>
        {pages > 1 && (
          <div className="flex items-center gap-3">
            <button disabled={page === 0} onClick={() => setPage((p) => p - 1)} className="hover:text-foreground disabled:opacity-40">
              上一页
            </button>
            <span className="font-mono tabular-nums">
              {page + 1} / {pages}
            </span>
            <button disabled={page >= pages - 1} onClick={() => setPage((p) => p + 1)} className="hover:text-foreground disabled:opacity-40">
              下一页
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
