# ruff: noqa: E501  （报表的表头和行是长字符串，折行反而难读）
"""归因评测：同一批"为什么"类问题，三种做法各跑一遍（方案见 evals/attribution/评测方案.md）。

  plain  现有问数流程直接回答（一条 SQL + 回答，不下钻）
  fixed  固定下钻流程（attribution.py：计划 → 分解 → 集中度 → 再拆一层 → 剥离 → 结论 + 数字核对）
  agent  模型拿着 SQL 工具自主下钻（attr_agent.py）

每例先把 retail_ops 复制一份、按 cases.json 注入变化，三组在同一份副本上并行跑；
每组的最终回答交给同一个抽取器（Haiku）整理成"原因列表"，再由代码和标准答案比对。

示例：
  # 不花钱：只跑固定流程的确定性部分（用标准的指标和时间段），比较排序方法 / 深度 / 集中度 / 剥离
  uv run python evals/attribution_eval.py --ablation --split dev
  # 三组对照（不加 --yes 只打印预估花费）
  uv run python evals/attribution_eval.py --split dev --models sonnet --max-cost 3 --yes
  # 汇总已有运行：命中率、数字一致率、成本、耗时、配对检验
  uv run python evals/attribution_eval.py --report <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from evalkit import store  # noqa: E402
from evalkit.stats import mcnemar_exact, percentile, wilson  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from datapilot import attribution as at  # noqa: E402
from datapilot import config  # noqa: E402
from datapilot.attr_agent import AttributionAgent  # noqa: E402
from datapilot.llm import ClaudeLLM  # noqa: E402
from datapilot.numcheck import check_numbers  # noqa: E402
from datapilot.semantic import load_layer  # noqa: E402
from datapilot.service import ChatService  # noqa: E402
from datapilot.store import MemoryStore  # noqa: E402
from evals.attribution.inject import apply, metric_value  # noqa: E402

DB = "retail_ops"
CASES = ROOT / "evals" / "attribution" / "cases.json"
WORK_DIR = config.DATA_DIR / "attribution_dbs"
ARMS = ("plain", "fixed", "agent")
JUDGE_MODEL = "haiku"
# 每例预估花费上限（美元），只用于开跑前的提示；实际花费逐次累计，超过 --max-cost 就停
EST = {"sonnet": {"plain": 0.012, "fixed": 0.02, "agent": 0.12}, "haiku": {"plain": 0.001, "fixed": 0.002, "agent": 0.012}}  # fmt: skip
DIMENSIONS = ("market", "country", "category", "customer_type", "value_tier", "size_band",
              "customer", "product", "invoice")  # fmt: skip


# ---------- 库的副本 ----------


def prepare_db(case: dict) -> Path:
    """复制一份库并注入；返回放这份库的目录（结构同 data/dev_databases）。"""
    src = config.DATA_DIR / "dev_databases" / DB
    root = WORK_DIR / f"{case['id']}-{os.getpid()}"  # 带进程号：两次评测同时跑也不互相覆盖
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(src, root / DB)
    path = root / DB / f"{DB}.sqlite"
    if case["kind"] == "injected":
        conn = sqlite3.connect(path)
        try:
            seeds = [case["seed"] + i for i in range(len(case["ops"]))]
            for i, seed in enumerate(seeds):  # 和生成评测集时一样：逐个操作、各用各的种子
                apply(conn, {**case, "ops": case["ops"][i : i + 1], "seed": seed}).finish()
            got = metric_value(conn, case["metric"], tuple(case["current"]))
        finally:
            conn.close()
        if abs(got - case["current_value"]) > 1e-4 * max(1.0, abs(got)):
            raise RuntimeError(
                f"{case['id']} 注入后的指标值 {got} 与评测集里的 {case['current_value']} 不一致"
            )
    return root


# ---------- 判定 ----------


def norm_cause(conds: list, layer) -> frozenset:
    """原因 = 一组（维度, 取值）。国家已经决定了市场，同时出现时去掉市场；取值转成库里的写法。"""
    zh = {v: k for k, v in layer.dimensions["country"].values.items()}
    out = set()
    for dim, value in conds:
        value = str(value).strip()
        if dim == "country":
            value = zh.get(value, value)
        if dim == "customer" and value.replace(".0", "").isdigit():
            value = value.replace(".0", "")
        out.add((dim, value))
    if any(d == "country" for d, _ in out):
        out = {(d, v) for d, v in out if d != "market"}
    return frozenset(out)


def score_causes(pred: list, case: dict, layer) -> dict:
    """第一原因命中：排第一的原因和任意一个标准答案（含等价说法）完全相同。
    前三命中：前三个里有一个命中。两个原因的题另记是否两个都在前三里。"""
    gold = [norm_cause(g, layer) for g in case["gold"]]
    got = [norm_cause(p, layer) for p in pred[:3] if p]
    hits = [g in gold for g in got]
    out = {"hit1": bool(hits[:1] and hits[0]), "hit3": any(hits)}
    if case.get("secondary"):
        both = [norm_cause(case["primary"], layer), norm_cause(case["secondary"], layer)]
        out["both"] = all(b in got for b in both)
    return out


class JCond(BaseModel):
    dimension: Literal[DIMENSIONS]  # type: ignore[valid-type]
    value: str


class JCause(BaseModel):
    conditions: list[JCond]


class JOut(BaseModel):
    causes: list[JCause]


JUDGE_SYSTEM = """You read an analyst's answer (in Chinese) to a question of the form "why did \
this metric change" and list the causes the answer states, in the order of importance the answer \
gives them (the cause it calls the main / first / biggest one comes first; otherwise the order \
of appearance). At most 3.

Rules:
- Only extract what the answer asserts as a reason for the change. Ignore segments mentioned \
only as background, as offsetting or opposite movements, or as explicitly not being a cause.
- If the answer gives no specific cause (it cannot tell, it only restates the total, or it says \
all segments moved proportionally), return an empty list.
- A cause is a list of conditions. Use two conditions only when the answer names a combination \
(e.g. "德国的节日派对品类"). If the answer attributes the change to one specific invoice and \
mentions that invoice's customer, country or product only to describe it, output the invoice \
condition alone. Likewise a specific customer or product alone, without its country.
- dimension / value vocabulary (use the stored value on the left of "="):
{vocabulary}
- customer: the customer id digits. product: the stock code. invoice: the invoice number \
(return invoices start with C)."""


def judge_prompt(layer) -> str:
    lines = []
    for did in ("market", "country", "category", "customer_type", "value_tier", "size_band"):
        d = layer.dimensions[did]
        vals = "; ".join(f"{k}={v}" for k, v in d.values.items()) or d.description
        lines.append(f"  {did} ({d.name}): {vals}")
    return JUDGE_SYSTEM.format(vocabulary="\n".join(lines))


async def judge(llm, layer, question: str, answer: str):
    if not answer.strip():
        return [], 0.0
    res = await llm.complete(
        judge_prompt(layer),
        [{"role": "user", "content": f"Question: {question}\n\nAnswer:\n{answer}"}],
        [],
        output_format=JOut,
    )
    causes = (
        [[[c.dimension, c.value] for c in cause.conditions] for cause in res.parsed.causes]
        if res.parsed
        else []
    )
    return causes, res.cost_usd


def _ids(tables: list[tuple[list[str], list[list]]]) -> set[str]:
    """结果表里的标识（编号、编码、发票号）：数字核对时不当成数据。"""
    out = set()
    for columns, rows in tables:
        for j, name in enumerate(columns):
            key = name.lower()
            is_id = any(k in key for k in ("id", "_no", "code", "invoice", "customer", "segment"))
            for r in rows:
                if j < len(r) and (isinstance(r[j], str) or (is_id and isinstance(r[j], int))):
                    out.add(str(r[j]))
    return out


def number_scores(text: str, tables: list[tuple[list[str], list[list]]], derived=None) -> dict:
    rows = [r for _, r in tables]
    ids = _ids(tables)
    strict = check_numbers(text, rows, derived, ids)
    lenient = check_numbers(text, rows, derived, ids, lenient=True)
    return {
        "numbers": strict.total,
        "matched_strict": strict.matched,
        "matched_lenient": lenient.matched,
        "from_sql": strict.from_sql,
        "derived": strict.derived,
        "unmatched": strict.unmatched,
    }


# ---------- 三组 ----------


class Counting:
    """包一层记花费和调用次数。"""

    def __init__(self, inner):
        self.inner, self.model = inner, inner.model
        self.cost, self.calls = 0.0, 0

    async def complete(self, system, messages, tools, output_format=None):
        res = await self.inner.complete(system, messages, tools, output_format)
        self.cost += res.cost_usd
        self.calls += 1
        return res


async def _ask(svc: ChatService, case: dict) -> dict:
    events = [e async for e in svc.ask(uuid.uuid4().hex, DB, case["question"])]
    by: dict[str, list] = {}
    for e in events:
        by.setdefault(e["event"], []).append(e["data"])
    return by


async def run_plain(llm, case: dict, layer) -> dict:
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm, attribution_enabled=False)
    by = await _ask(svc, case)
    table = (by.get("table") or [None])[0]
    answer = (by.get("answer") or [{}])[0].get("text", "")
    tables = [(table["columns"], table["rows"])] if table else []
    return {
        "answer": answer,
        "error": (by.get("error") or [{}])[0].get("message", ""),
        "sql": [(by.get("sql") or [{}])[0].get("sql", "")],
        "structured": None,
        **number_scores(answer, tables),
    }


async def run_fixed(llm, case: dict, layer) -> dict:
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)
    by = await _ask(svc, case)
    attr = (by.get("attribution") or [None])[0]
    ans = (by.get("answer") or [{}])[0]
    answer = ans.get("text", "")
    out = {
        "answer": answer,
        "error": (by.get("error") or [{}])[0].get("message", ""),
        "sql": [e["sql"] for e in attr["evidence"]] if attr else [],
        "structured": None,
        "fell_back": attr is None,
    }
    if attr is None:  # 没走归因（计划映射不了）：按普通问数的结果记
        table = (by.get("table") or [None])[0]
        tables = [(table["columns"], table["rows"])] if table else []
        return {**out, **number_scores(answer, tables)}
    tables = [(e["columns"], e["rows"]) for e in attr["evidence"]]
    derived = [f["value"] for f in attr["facts"]] + attr["label_numbers"]
    scope = attr["scope"]
    out.update(
        structured=[[[c["dimension"], c["value"]] for c in cause["conditions"]] for cause in attr["causes"]],
        plan_ok=scope["metric"] == case["metric"]
        and scope["current"] == case["current"]
        and scope["base"] == case["base"],
        plan={"metric": scope["metric"], "current": scope["current"], "base": scope["base"]},
        answer_source=ans.get("source", ""),
        stop_reason=attr["stop_reason"],
        **number_scores(answer, tables, derived),
    )  # fmt: skip
    return out


async def run_agent(llm, case: dict, layer) -> dict:
    run = await AttributionAgent()(llm, layer, case["question"])
    tables = [(q["columns"], q["rows"]) for q in run.queries if "rows" in q]
    return {
        "answer": run.conclusion,
        "error": "" if run.submitted else f"没有提交答案（{run.stop_reason}）",
        "sql": [q["sql"] for q in run.queries],
        "structured": run.causes,
        "steps": run.steps,
        **number_scores(run.conclusion, tables),
    }


RUNNERS = {"plain": run_plain, "fixed": run_fixed, "agent": run_agent}


async def run_case(case: dict, models: list[str], arms: list[str], clients: dict, layer, spent):
    root = prepare_db(case)
    config.DB_DIR = root  # 这一例的三组都读这份副本；各例顺序执行
    rows = []

    async def one(model: str, arm: str) -> dict:
        llm = Counting(ClaudeLLM(config.MODELS[model], client=clients["api"]))
        start = time.perf_counter()
        try:
            out = await RUNNERS[arm](llm, case, layer)
        except Exception as e:  # 单例失败记下来，不让整次评测停掉
            out = {"answer": "", "error": f"{type(e).__name__}: {e}", "sql": [], "structured": None}
        latency = time.perf_counter() - start
        jl = Counting(ClaudeLLM(config.MODELS[JUDGE_MODEL], client=clients["api"]))
        try:
            extracted, jcost = await judge(jl, layer, case["question"], out["answer"])
        except Exception as e:
            extracted, jcost = [], 0.0
            out["judge_error"] = f"{type(e).__name__}: {e}"
        row = {
            "item_id": case["id"],  # evalkit 的结果行约定：item_id + variant 唯一确定一行
            "variant": f"{model}/{arm}",
            "case": case["id"],
            "split": case["split"],
            "kind": case["kind"],
            "type": case["type"],
            "metric": case["metric"],
            "model": model,
            "arm": arm,
            "question": case["question"],
            "gold": case["gold"],
            "extracted": extracted,
            "text": score_causes(extracted, case, layer),
            "struct": score_causes(out["structured"], case, layer) if out.get("structured") is not None else None,
            "cost_usd": round(llm.cost, 6),
            "judge_cost_usd": round(jcost, 6),
            "llm_calls": llm.calls,
            "latency_s": round(latency, 2),
            **out,
        }  # fmt: skip
        row["passed"] = row["text"]["hit1"]
        spent[0] += llm.cost + jcost
        return row

    try:
        rows = await asyncio.gather(*(one(m, a) for m in models for a in arms))
    finally:
        config.DB_DIR = config.DATA_DIR / "dev_databases"
        shutil.rmtree(root, ignore_errors=True)
    return rows


# ---------- 汇总 ----------


def _rate(k: int, n: int) -> str:
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.1%}（{lo:.0%}–{hi:.0%}）"


def summarize(rows: list[dict]) -> dict:
    out = {}
    groups = sorted({(r["model"], r["arm"]) for r in rows})
    for model, arm in groups:
        rs = [r for r in rows if r["model"] == model and r["arm"] == arm]
        n = len(rs)
        nums = sum(r.get("numbers", 0) for r in rs)
        lat = [r["latency_s"] for r in rs]
        st = [r for r in rs if r.get("struct")]
        out[f"{model}/{arm}"] = {
            "n": n,
            "hit1": sum(r["text"]["hit1"] for r in rs),
            "hit3": sum(r["text"]["hit3"] for r in rs),
            "struct_n": len(st),
            "struct_hit1": sum(r["struct"]["hit1"] for r in st),
            "struct_hit3": sum(r["struct"]["hit3"] for r in st),
            "numbers": nums,
            "matched_strict": sum(r.get("matched_strict", 0) for r in rs),
            "matched_lenient": sum(r.get("matched_lenient", 0) for r in rs),
            "answers_all_matched": sum(
                1 for r in rs if r.get("numbers", 0) and r["matched_strict"] == r["numbers"]
            ),
            "answers_with_numbers": sum(1 for r in rs if r.get("numbers", 0)),
            "cost_mean": sum(r["cost_usd"] for r in rs) / n,
            "cost_total": sum(r["cost_usd"] for r in rs),
            "latency_p50": percentile(lat, 0.5),
            "latency_p95": percentile(lat, 0.95),
            "calls_mean": sum(r["llm_calls"] for r in rs) / n,
            "errors": sum(1 for r in rs if r.get("error")),
            "plan_ok": sum(1 for r in rs if r.get("plan_ok")) if arm == "fixed" else None,
            "fell_back": sum(1 for r in rs if r.get("fell_back")) if arm == "fixed" else None,
            "template": sum(1 for r in rs if r.get("answer_source") == "template") if arm == "fixed" else None,
            "rewrite": sum(1 for r in rs if r.get("answer_source") == "rewrite") if arm == "fixed" else None,
        }  # fmt: skip
    return out


def paired(rows: list[dict], model: str, a: str, b: str, key: str = "hit1") -> dict:
    ra = {r["case"]: r["text"][key] for r in rows if r["model"] == model and r["arm"] == a}
    rb = {r["case"]: r["text"][key] for r in rows if r["model"] == model and r["arm"] == b}
    common = sorted(set(ra) & set(rb))
    only_a = sum(1 for c in common if ra[c] and not rb[c])
    only_b = sum(1 for c in common if rb[c] and not ra[c])
    return {
        "n": len(common),
        "only_a": only_a,
        "only_b": only_b,
        "p": mcnemar_exact(only_a, only_b),
    }


def report(rows: list[dict], title: str = "") -> str:
    lines = [f"## {title}" if title else ""]
    for split in ("all", *sorted({r["split"] for r in rows})):
        rs = rows if split == "all" else [r for r in rows if r["split"] == split]
        if split != "all" and len({r["split"] for r in rows}) < 2:
            continue
        s = summarize(rs)
        lines.append(f"\n### {split}（{len({r['case'] for r in rs})} 例）\n")
        lines.append(
            "| 模型 / 做法 | 第一原因命中 | 前三命中 | 结构化第一 / 前三 | 数字一致（严格） | 含一步运算 | 回答全部对上 | 单次成本 | P50 / P95 耗时 | 模型调用 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for k, v in s.items():
            struct = (
                f"{v['struct_hit1']}/{v['struct_n']} · {v['struct_hit3']}/{v['struct_n']}"
                if v["struct_n"]
                else "—"
            )
            num = (
                f"{v['matched_strict']}/{v['numbers']} = {v['matched_strict'] / v['numbers']:.1%}"
                if v["numbers"]
                else "—"
            )
            len_ = f"{v['matched_lenient'] / v['numbers']:.1%}" if v["numbers"] else "—"
            lines.append(
                f"| {k} | {_rate(v['hit1'], v['n'])} | {_rate(v['hit3'], v['n'])} | {struct} | {num} | {len_} "
                f"| {v['answers_all_matched']}/{v['answers_with_numbers']} | ${v['cost_mean']:.4f} "
                f"| {v['latency_p50']:.1f}s / {v['latency_p95']:.1f}s | {v['calls_mean']:.1f} |"
            )
        for model in sorted({r["model"] for r in rs}):
            for a, b in (("plain", "fixed"), ("agent", "fixed"), ("plain", "agent")):
                for key in ("hit1", "hit3"):
                    p = paired(rs, model, a, b, key)
                    if p["n"]:
                        lines.append(
                            f"- {model} {a} → {b}（{key}，{p['n']} 例配对）：{a} 独对 {p['only_a']}、"
                            f"{b} 独对 {p['only_b']}，McNemar p = {p['p']:.4f}"
                        )
        fx = {k: v for k, v in s.items() if k.endswith("/fixed")}
        for k, v in fx.items():
            lines.append(
                f"- {k}：计划（指标 + 两个时间段）与标准一致 {v['plan_ok']}/{v['n']}，退回普通问数 {v['fell_back']}，"
                f"结论重写 {v['rewrite']}、改用模板 {v['template']}，出错 {v['errors']}"
            )
    return "\n".join(lines)  # fmt: skip


# ---------- 消融（不花钱） ----------

ABLATIONS = {
    "默认（超额 − 正常波动 + 两层 + 集中度 + 剥离）": {},
    "不扣正常波动、不做进出对冲": {"noise_k": 0.0, "entity_exchange": False},
    "波动按时间间隔放大（sqrt 间隔月数）": {"noise_gap": True},
    "客户 / 商品不做进出对冲": {"entity_exchange": False},
    "按原始贡献排序": {"ranking": "raw"},
    "只拆一层": {"max_depth": 1},
    "不查客户 / 商品 / 单据集中度": {"entities": False},
    "不剥离（直接取排序前三）": {"peel": False},
    "只用 国家 + 品类 两个维度": {"dimensions": ("country", "category")},
}


def ablation(cases: list[dict], only: str | None = None) -> str:
    layer = load_layer(DB)
    results = {name: [] for name in ABLATIONS}
    timings = []
    for case in cases:
        root = prepare_db(case)
        try:
            path = root / DB / f"{DB}.sqlite"
            scope = at.Scope(case["metric"], tuple(case["base"]), tuple(case["current"]))
            for name, kw in ABLATIONS.items():
                if only and name != only:
                    continue
                t = time.perf_counter()
                res = at.run_attribution(layer, path, scope, cfg=at.AttrConfig(**kw))
                timings.append((time.perf_counter() - t, len(res.evidence)))
                pred = [
                    [[c["dimension"], c["value"]] for c in cause.conditions] for cause in res.causes
                ]
                results[name].append({"case": case["id"], "split": case["split"], "kind": case["kind"],
                                      "type": case["type"], "pred": pred, **score_causes(pred, case, layer)})  # fmt: skip
        finally:
            shutil.rmtree(root, ignore_errors=True)
    lines = ["| 方案 | 第一原因命中 | 前三命中 | 两个原因都找到 |", "|---|---|---|---|"]
    base = {r["case"]: r for r in results[next(iter(ABLATIONS))]}
    for name, rs in results.items():
        if not rs:
            continue
        both = [r for r in rs if "both" in r]
        row = f"| {name} | {_rate(sum(r['hit1'] for r in rs), len(rs))} | {_rate(sum(r['hit3'] for r in rs), len(rs))} | {sum(r['both'] for r in both)}/{len(both)} |"
        if name != next(iter(ABLATIONS)) and base:
            b = sum(1 for r in rs if base[r["case"]]["hit1"] and not r["hit1"])
            c = sum(1 for r in rs if r["hit1"] and not base[r["case"]]["hit1"])
            row += f" 默认独对 {b}、它独对 {c}，p = {mcnemar_exact(b, c):.4f}"
        lines.append(row)
    lines.append("")
    for r in results[next(iter(ABLATIONS))]:
        if not r["hit1"]:
            lines.append(f"- 未命中 {r['case']}（{r['type']}）：给出 {r['pred'][:2]}")
    if timings:
        secs = [t for t, _ in timings]
        lines.append(f"- 确定性部分耗时 P50 {percentile(secs, 0.5):.2f}s / P95 {percentile(secs, 0.95):.2f}s，"
                     f"每次 {sum(n for _, n in timings) / len(timings):.0f} 条 SQL")  # fmt: skip
    return "\n".join(lines)


# ---------- 入口 ----------


async def main_async(args) -> None:
    import anthropic

    load_dotenv(ROOT / ".env")
    cases = [
        c
        for c in json.loads(CASES.read_text(encoding="utf-8"))
        if args.split in ("all", c["split"])
    ]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    cases = cases[: args.limit] if args.limit else cases
    models, arms = args.models.split(","), args.arms.split(",")
    est = sum(EST[m][a] for m in models for a in arms) * len(cases)
    print(
        f"{len(cases)} 例 × 模型 {models} × 做法 {arms}，预估花费上限 ${est:.2f}，硬上限 ${args.max_cost:.2f}"
    )
    if not args.yes:
        print("加 --yes 才会真正调用模型")
        return
    run_id = args.resume or store.new_run_id(args.tag)
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    done = set()
    if args.resume:
        done = {(r["case"], r["model"], r["arm"]) for r in store.load_rows(run_dir)}
    store.write_json(run_dir / "config.json", {
        "run_id": run_id, "split": args.split, "models": models, "arms": arms, "judge": JUDGE_MODEL,
        "cases_file": str(CASES.relative_to(ROOT)), "git_commit": store.git_commit(ROOT),
    })  # fmt: skip
    layer = load_layer(DB)
    clients = {"api": anthropic.AsyncAnthropic(max_retries=6)}
    spent = [0.0]
    for i, case in enumerate(cases, 1):
        todo_models = [m for m in models if any((case["id"], m, a) not in done for a in arms)]
        if not todo_models:
            continue
        if spent[0] >= args.max_cost:
            print(f"花费已到 ${spent[0]:.2f}，达到上限，停止（可用 --resume {run_id} 续跑）")
            break
        rows = await run_case(case, todo_models, arms, clients, layer, spent)
        for r in rows:
            store.append_row(run_dir, r)
        marks = " ".join(
            f"{r['model'][0]}/{r['arm']}:{'✓' if r['text']['hit1'] else '✗'}" for r in rows
        )
        print(f"[{i}/{len(cases)}] {case['id']} {marks}  累计 ${spent[0]:.3f}", flush=True)
    rows = store.load_rows(run_dir)
    store.write_json(
        run_dir / "summary.json", {"summary": summarize(rows), "spent_usd": round(spent[0], 4)}
    )
    print(f"run_id={run_id}  本次花费 ${spent[0]:.4f}")
    print(report(rows, run_id))  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--models", default="sonnet")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑这些例子（逗号分隔的 id）")
    ap.add_argument("--max-cost", type=float, default=2.0)
    ap.add_argument("--tag", default="attr")
    ap.add_argument("--resume", default="")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--ablation", action="store_true", help="只跑固定流程的确定性部分，不调用模型")
    ap.add_argument("--report", nargs="*", help="汇总已有运行（可给多个 run_id，合并统计）")
    args = ap.parse_args()
    if args.report:
        rows = [r for rid in args.report for r in store.load_rows(config.RUNS_DIR / rid)]
        print(report(rows, " + ".join(args.report)))
        return
    if args.ablation:
        cases = [
            c
            for c in json.loads(CASES.read_text(encoding="utf-8"))
            if args.split in ("all", c["split"])
        ]
        if args.only:
            cases = [c for c in cases if c["id"] in args.only.split(",")]
        print(f"{args.split}：{len(cases)} 例（固定流程的确定性部分，指标和时间段用标准值）\n")
        print(ablation(cases))
        return
    asyncio.run(main_async(args))  # fmt: skip


if __name__ == "__main__":
    main()
