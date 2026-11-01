"use client";

import { CaretRight, Moon, Sun } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import { ChartView } from "@/components/chart-view";
import { SqlBlock } from "@/components/sql-block";
import { TopNav } from "@/components/top-nav";
import data from "@/data/eval.json";
import { toggleTheme, useIsDark } from "@/lib/theme";
import { cn } from "@/lib/utils";

type Stats = {
  n: number;
  ex: number;
  ci: number[];
  cost_per_q: number;
  p50: number;
  p95: number;
  by_difficulty: Record<string, number>;
  p_vs_prev?: number;
  p_vs_e4?: number;
};

const pct = (x: number, d = 1) => `${(x * 100).toFixed(d)}%`;
const pval = (p?: number) => (p === undefined ? "" : p < 0.001 ? "p<0.001" : `p=${p.toFixed(3)}`);

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-medium tracking-tight">{title}</h2>
        {note && <p className="mt-1 max-w-[70ch] text-sm leading-relaxed text-muted-foreground">{note}</p>}
      </div>
      {children}
    </section>
  );
}

function Kpi({ value, label, sub }: { value: string; label: string; sub: string }) {
  return (
    <div className="border-l-2 border-primary/60 pl-4">
      <div className="font-mono text-3xl font-medium tabular-nums tracking-tight">{value}</div>
      <div className="mt-1 text-sm">{label}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>
    </div>
  );
}

function Table({ head, rows, align }: { head: string[]; rows: (string | number)[][]; align?: ("l" | "r")[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-surface text-muted-foreground">
          <tr>
            {head.map((h, i) => (
              <th key={i} className={cn("whitespace-nowrap px-3 py-2 font-medium", align?.[i] === "r" ? "text-right" : "text-left")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((r, ri) => (
            <tr key={ri}>
              {r.map((c, ci) => (
                <td key={ci} className={cn("whitespace-nowrap px-3 py-2", align?.[ci] === "r" && "text-right font-mono tabular-nums")}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Ladder() {
  const ladder = data.ladder as unknown as { key: string; label: string; run_id: string; sonnet: Stats; haiku: Stats }[];
  const chart = {
    columns: ["实验", "Sonnet", "Haiku"],
    rows: ladder.map((l) => [l.key, +(l.sonnet.ex * 100).toFixed(1), +(l.haiku.ex * 100).toFixed(1)]),
    truncated: false,
  };
  return (
    <>
      <ChartView spec={{ type: "bar", x: "实验", y: ["Sonnet", "Haiku"] }} data={chart} />
      <Table
        head={["实验", "说明", "Sonnet EX", "相对上一步", "Haiku EX", "相对上一步", "Sonnet 单题成本", "Sonnet P50"]}
        align={["l", "l", "r", "r", "r", "r", "r", "r"]}
        rows={ladder.map((l) => [
          l.key,
          l.label,
          pct(l.sonnet.ex),
          l.key === "Agent" ? `vs E4 ${pval(l.sonnet.p_vs_e4)}` : pval(l.sonnet.p_vs_prev),
          pct(l.haiku.ex),
          l.key === "Agent" ? `vs E4 ${pval(l.haiku.p_vs_e4)}` : pval(l.haiku.p_vs_prev),
          `$${l.sonnet.cost_per_q.toFixed(4)}`,
          `${l.sonnet.p50}s`,
        ])}
      />
    </>
  );
}

function Models() {
  const f = data.final as unknown as Record<string, Stats & { mean?: number; std?: number; runs: number[] | null; p_vs_haiku?: number; haiku_paired_ex?: number }>;
  const s100 = data.same100 as unknown as Record<string, Stats>;
  const cn = (data as unknown as { cn_models: (Stats & { name: string; p_vs_haiku: number; p_vs_sonnet: number; haiku_paired_ex: number; sonnet_paired_ex: number })[] }).cn_models;
  return (
    <div className="grid gap-8">
      <div className="space-y-2">
        <h3 className="text-sm text-muted-foreground">最终配置 E4，500 题</h3>
        <Table
          head={["模型", "EX", "simple", "moderate", "challenging", "单题成本"]}
          align={["l", "r", "r", "r", "r", "r"]}
          rows={(["haiku", "sonnet", "opus", "qwen"] as const).map((m) => [
            m === "opus" ? "Opus（疑似污染）" : m === "qwen" ? "qwen2.5-coder:7b（本地）" : m[0].toUpperCase() + m.slice(1),
            f[m].mean !== undefined ? `${pct(f[m].mean!)} ± ${(f[m].std! * 100).toFixed(2)}` : pct(f[m].ex),
            pct(f[m].by_difficulty.simple, 0),
            pct(f[m].by_difficulty.moderate, 0),
            pct(f[m].by_difficulty.challenging, 0),
            m === "qwen" ? "本地" : `$${f[m].cost_per_q.toFixed(4)}`,
          ])}
        />
        <p className="text-xs text-muted-foreground">
          Haiku、Sonnet 为 3 次运行均值 ± 标准差（百分点）；本地模型跑 1 次，与 Haiku 首次运行逐题配对：{pct(f.qwen.haiku_paired_ex!)} vs {pct(f.qwen.ex)}，{pval(f.qwen.p_vs_haiku!)}。
        </p>
      </div>
      <div className="space-y-2">
        <h3 className="text-sm text-muted-foreground">国产 API 模型，最终配置 E4，500 题</h3>
        <Table
          head={["模型", "EX", "95% CI", "simple", "moderate", "challenging", "单题成本", "P95 延迟", "vs Haiku", "vs Sonnet"]}
          align={["l", "r", "r", "r", "r", "r", "r", "r", "r", "r"]}
          rows={cn.map((m) => [
            m.name,
            pct(m.ex),
            `${pct(m.ci[0], 0)} - ${pct(m.ci[1], 0)}`,
            pct(m.by_difficulty.simple, 0),
            pct(m.by_difficulty.moderate, 0),
            pct(m.by_difficulty.challenging, 0),
            `$${m.cost_per_q.toFixed(4)}`,
            `${m.p95}s`,
            `${pct(m.haiku_paired_ex)} · ${pval(m.p_vs_haiku)}`,
            `${pct(m.sonnet_paired_ex)} · ${pval(m.p_vs_sonnet)}`,
          ])}
        />
        <p className="text-xs text-muted-foreground">
          经 OpenAI 兼容接口接入，JSON 模式、temperature 0，各跑 1 次；配对对象是 Haiku、Sonnet 的 E4 首次运行（括号前为对方在同题上的 EX）。逐字复现标准答案的题数低于 Claude，没有污染迹象。
        </p>
      </div>
      <div className="space-y-2">
        <h3 className="text-sm text-muted-foreground">同 100 题（分层抽样），含本地开源模型</h3>
        <Table
          head={["模型", "EX", "95% CI", "P50 延迟", "单题成本"]}
          align={["l", "r", "r", "r", "r"]}
          rows={Object.entries(s100).map(([m, s]) => [
            m,
            pct(s.ex, 0),
            `${pct(s.ci[0], 0)} - ${pct(s.ci[1], 0)}`,
            `${s.p50}s`,
            m.startsWith("qwen") ? "本地" : `$${s.cost_per_q.toFixed(4)}`,
          ])}
        />
        <p className="text-xs text-muted-foreground">qwen2.5-coder:7b 经 Ollama 运行于 M4 16GB；与 Haiku 的差距在 100 题上不显著。</p>
      </div>
    </div>
  );
}

function Routing() {
  type R = { ex_mean: number; ex_std?: number; escalated_mean?: number; cost_mean: number };
  const r = (data as unknown as { route: { haiku: R; sonnet: R; oracle: R; rules: Record<string, R>; live: { ex: number; escalated: number; cost: number } } }).route;
  const names: Record<string, string> = { r1_any_fix: "r1：触发纠错就升级", r2_unresolved: "r2：纠错后仍有问题才升级", r3_fix_or_complex: "r3：r1 或关联 ≥ 3 张表" };
  const ratio = (c: number) => `${Math.round((c / r.sonnet.cost_mean) * 100)}%`;
  return (
    <>
      <Table
        head={["方案", "EX", "升级题数", "500 题成本", "成本 / 全 Sonnet"]}
        align={["l", "r", "r", "r", "r"]}
        rows={[
          ["全用 Haiku", pct(r.haiku.ex_mean), "0", `$${r.haiku.cost_mean.toFixed(3)}`, ratio(r.haiku.cost_mean)],
          ...Object.entries(r.rules).map(([k, v]) => [
            `${names[k]}（回放）`,
            `${pct(v.ex_mean)} ± ${((v.ex_std ?? 0) * 100).toFixed(2)}`,
            Math.round(v.escalated_mean ?? 0).toString(),
            `$${v.cost_mean.toFixed(3)}`,
            ratio(v.cost_mean),
          ]),
          ["r1（实跑）", pct(r.live.ex), r.live.escalated.toString(), `$${r.live.cost.toFixed(3)}`, ratio(r.live.cost)],
          ["全用 Sonnet", pct(r.sonnet.ex_mean), "500", `$${r.sonnet.cost_mean.toFixed(3)}`, "100%"],
          ["理想路由（预知哪题会错）", pct(r.oracle.ex_mean), Math.round(r.oracle.escalated_mean ?? 0).toString(), `$${r.oracle.cost_mean.toFixed(3)}`, ratio(r.oracle.cost_mean)],
        ]}
      />
      <p className="text-xs text-muted-foreground">
        实跑与回放一致。按失败信号路由只能追回 Haiku 与 Sonnet 差距的 1/3 到一半：Haiku 的错题大多执行成功、没有报错，运行时看不出来；理想路由能到 {pct(r.oracle.ex_mean)}，提升空间在更好的“会不会错”判断器。
      </p>
    </>
  );
}

function Chinese() {
  const z = (data as unknown as { zh100: { models: Record<string, { en: number; zh: number; en_only: number; zh_only: number; p: number }> } }).zh100;
  return (
    <>
      <Table
        head={["模型", "英文", "中文", "英文独对 / 中文独对", "配对检验"]}
        align={["l", "r", "r", "r", "r"]}
        rows={Object.entries(z.models).map(([m, v]) => [m[0].toUpperCase() + m.slice(1), pct(v.en, 0), pct(v.zh, 0), `${v.en_only} / ${v.zh_only}`, pval(v.p)])}
      />
      <p className="text-xs text-muted-foreground">
        中英文没有显著差别，两个模型方向还相反。取值映射题中英文持平：增强 schema 里本来就带取值说明。中文特有的失败主要来自问法歧义；首轮有 3 条译文偏离原意，已修正后重跑（问题日志 P18）。
      </p>
    </>
  );
}

function ShopOps() {
  const s = (data as unknown as { shop_ops: { models: { model: string; with_ev: number[]; no_ev: number[]; with_mean: number; no_mean: number }[] } }).shop_ops;
  const chart = {
    columns: ["模型", "有口径说明", "去掉口径说明"],
    rows: s.models.map((m) => [m.model, +(m.with_mean * 100).toFixed(1), +(m.no_mean * 100).toFixed(1)]),
    truncated: false,
  };
  return (
    <>
      <ChartView spec={{ type: "bar", x: "模型", y: ["有口径说明", "去掉口径说明"] }} data={chart} />
      <Table
        head={["模型", "有口径说明", "去掉口径说明", "下降"]}
        align={["l", "r", "r", "r"]}
        rows={s.models.map((m) => [
          m.model,
          m.with_ev.length > 1 ? `${pct(m.with_mean)}（${m.with_ev.length} 次）` : pct(m.with_mean),
          m.no_ev.length > 1 ? `${pct(m.no_mean)}（${m.no_ev.length} 次）` : pct(m.no_mean),
          `${((m.with_mean - m.no_mean) * 100).toFixed(1)} 点`,
        ])}
      />
      <p className="text-xs text-muted-foreground">
        40 题中 25 题依赖口径（有效订单、销售额、退款率等）；标准答案不变。去掉口径后，模型普遍把销售额算得更严（排除已退款），与公司口径不同。第 36 题（价格分档）两种条件下都判错，是输出形状不同导致的评测缺陷。
      </p>
    </>
  );
}

type Case = (typeof data.failure.cases)[number];

function CaseRow({ c }: { c: Case }) {
  const [open, setOpen] = useState(false);
  const names = data.failure.names as Record<string, string>;
  return (
    <li className="py-3">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-start gap-2 text-left" aria-expanded={open}>
        <CaretRight size={12} className={cn("mt-1.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        <span className="w-14 shrink-0 font-mono text-xs leading-6 text-muted-foreground">q{c.qid}</span>
        <span className="flex-1 text-sm leading-6">{c.question}</span>
        <span
          className={cn(
            "shrink-0 rounded-md px-2 py-0.5 text-xs",
            c.category === "B" ? "bg-primary/15 text-primary" : "bg-accent text-muted-foreground",
          )}
        >
          {names[c.category]}
        </span>
      </button>
      {open && (
        <div className="mt-3 space-y-3 pl-[5.5rem] text-sm">
          {c.evidence && <p className="text-muted-foreground">evidence：{c.evidence}</p>}
          <p className="leading-relaxed">
            <span className="text-muted-foreground">判定理由：</span>
            {c.reason}
          </p>
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="space-y-1.5">
              <div className="text-xs text-muted-foreground">标准 SQL</div>
              <SqlBlock sql={c.gold_sql} />
            </div>
            <div className="space-y-1.5">
              <div className="text-xs text-muted-foreground">模型 SQL</div>
              {c.pred_sql ? <SqlBlock sql={c.pred_sql} /> : <p className="text-muted-foreground">（没有生成 SQL）</p>}
            </div>
          </div>
        </div>
      )}
    </li>
  );
}

function Failures() {
  const f = data.failure;
  const names = f.names as Record<string, string>;
  const counts = f.counts as Record<string, number>;
  const [filter, setFilter] = useState<string | null>(null);
  const cases = useMemo(() => f.cases.filter((c) => !filter || c.category === filter), [f.cases, filter]);
  const order = Object.keys(names).sort((a, b) => counts[b] - counts[a]);
  const total = f.cases.length;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setFilter(null)}
          className={cn("rounded-lg border px-3 py-1.5 text-sm", !filter ? "border-primary/50 text-foreground" : "text-muted-foreground")}
        >
          全部 <span className="font-mono">{total}</span>
        </button>
        {order.map((k) => (
          <button
            key={k}
            onClick={() => setFilter(k)}
            className={cn(
              "rounded-lg border px-3 py-1.5 text-sm",
              filter === k ? "border-primary/50 text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {names[k]} <span className="font-mono">{counts[k]}</span>
            <span className="ml-1 text-xs text-muted-foreground">{pct(counts[k] / total, 0)}</span>
          </button>
        ))}
      </div>
      <ul className="divide-y rounded-lg border px-4">
        {cases.map((c) => (
          <CaseRow key={c.qid} c={c} />
        ))}
      </ul>
    </div>
  );
}

export default function EvalPage() {
  const dark = useIsDark();
  const ladder = data.ladder as unknown as { sonnet: Stats; haiku: Stats }[];
  const f = data.final as unknown as Record<string, { mean?: number }>;
  const sc = data.selfcheck;
  const cache = data.cache;

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

      <main className="mx-auto max-w-6xl space-y-14 px-4 py-10 md:px-8">
        <div>
          <h1 className="text-2xl font-medium tracking-tight">评测看板</h1>
          <p className="mt-2 max-w-[70ch] text-sm leading-relaxed text-muted-foreground">
            BIRD Mini-Dev 500 题、11 个真实数据库，指标为执行准确率（EX），评测器与官方判分逐题对齐。全部数字由运行记录生成，运行 ID 见
            docs/numbers.md。
          </p>
        </div>

        <div className="grid gap-8 sm:grid-cols-3">
          <Kpi
            value={`${pct(ladder[0].haiku.ex)} → ${pct(f.haiku.mean!)}`}
            label="Haiku 执行准确率"
            sub={`取值核对 + 执行纠错，McNemar p=${data.haiku_e0_to_e4_p}，3 次均值`}
          />
          <Kpi
            value={pct(data.failure.counts.B / data.failure.cases.length, 0)}
            label="失败题中标准答案错"
            sub={`${data.failure.cases.length} 道分层抽样，人工判定；审计论文报告 52.8%`}
          />
          <Kpi
            value={`${(cache.hit_latency_p50 * 1000).toFixed(1)} ms`}
            label="缓存命中延迟（P50）"
            sub={`未命中 ${cache.miss_latency_p50.toFixed(2)} s；重复问题命中 ${pct(cache.repeat_hit_rate)}，构造请求`}
          />
        </div>

        <Section
          title="逐项改进"
          note="同一批 500 题，每一步与上一步做配对检验。改进在 Haiku 上显著，在 Sonnet 上不显著：它很少犯取值和报错类错误。Agent 准确率与固定流程相同，但更贵更慢。"
        >
          <Ladder />
        </Section>

        <Section title="模型对比" note="Opus 有 241/500 条预测逐字复现标准答案，疑似训练数据污染，数字仅供参考。">
          <Models />
        </Section>

        <Section
          title="智能路由"
          note="Haiku 先答，触发纠错等失败信号才交给 Sonnet 从头重跑。三条规则事先定好、全部报告；离线回放用已有的 3 组配对运行，r1 另外实跑一次验证。"
        >
          <Routing />
        </Section>

        <Section
          title="中文提问"
          note="BIRD 同批 100 题翻译成中文问法（库里取值保持英文，29 题需要取值映射），与英文原题按题配对。"
        >
          <Chinese />
        </Section>

        <Section
          title="电商经营分析（模拟数据）"
          note="BIRD 是英文公开基准，缺业务场景；这里用固定种子生成的模拟电商库（6000 订单、611 售后）出了 40 道中文经营分析题。口径写清楚时便宜模型也能到 95%；把口径说明去掉，所有模型都掉 30 多个点，换更贵的模型也补不回来。"
        >
          <ShopOps />
        </Section>

        <Section
          title="失败分析"
          note={`从 Sonnet 最终配置的 ${data.failure.n_failures} 道失败题中分层随机抽 50 道，先定判定标准再逐题判定。点开可看两条 SQL 和判定理由。`}
        >
          <Failures />
        </Section>

        <Section title="评测可信度" note="不先证明评测器可靠，后面的数字都没有意义。">
          <Table
            head={["自检", "结果", "说明"]}
            align={["l", "r", "l"]}
            rows={[
              ["标准 SQL 对比自身", pct(sc.selfcheck_gold_self_acc), "理论上限：两题标准 SQL 超过 30 秒预算"],
              ["打乱行顺序", pct(sc.selfcheck_shuffle_acc), "3 题标准答案用了无 ORDER BY 的 LIMIT"],
              ["恒返回 SELECT 1", pct(sc.selfcheck_select1_acc), "空 Agent 下限"],
              ["与官方判分逐题对照", "0 / 1500", "三组自检全部一致"],
            ]}
          />
        </Section>
      </main>
    </div>
  );
}
