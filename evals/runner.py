"""评测 runner：模型 × 流程 × effort × 重复次数，逐题生成 SQL 并按官方口径算 EX。

示例：
  # 用标准答案代替模型，自检整条评测管线（不调用 API、不花钱）
  uv run python evals/runner.py --models gold --sample 20

  # 试跑：三档模型，按难度分层抽 20 题
  uv run python evals/runner.py --models haiku,sonnet,opus --sample 20 --tag trial

  # 断点续跑：已完成的（题, 模型, 流程, effort, 重复）不再调用模型
  uv run python evals/runner.py --models haiku,sonnet,opus --resume <run_id>

不加 --yes 只打印预估花费。每道题跑完立即追加到 runs/<run_id>/results.jsonl。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datapilot import config  # noqa: E402
from datapilot.baseline import SQLAnswer, build_prompt, generate_sql  # noqa: E402
from datapilot.ex import evaluate_one  # noqa: E402
from datapilot.llm import LLM, ScriptedLLM  # noqa: E402
from evals.report import render, summarize  # noqa: E402

PIPELINES = {"baseline": generate_sql}
# 每题输出 token 预估（SQL + adaptive thinking）。试跑后用实测值替换。
EST_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class Config:
    model: str
    pipeline: str
    effort: str
    repeat: int


def key_of(row: dict) -> tuple:
    return (row["question_id"], row["model"], row["pipeline"], row["effort"], row["repeat"])


def load_done(run_dir: Path) -> set[tuple]:
    """已完成的题；llm_error（网络、限流等）不算完成，续跑时重试。"""
    path = run_dir / "results.jsonl"
    if not path.exists():
        return set()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {key_of(r) for r in rows if r["status"] != "llm_error"}


def stratified_sample(questions: list[dict], n: int, seed: int = 0) -> list[dict]:
    """按难度分层抽样，各层按原比例分配名额，固定种子保证可复现。"""
    rng = random.Random(seed)
    by_diff: dict[str, list[dict]] = {}
    for q in questions:
        by_diff.setdefault(q["difficulty"], []).append(q)
    picked = []
    for _, qs in sorted(by_diff.items()):
        k = round(n * len(qs) / len(questions))
        picked += rng.sample(qs, k)
    return sorted(picked, key=lambda q: q["question_id"])


async def run_one(q: dict, cfg: Config, llm: LLM) -> dict:
    path = config.db_path(q["db_id"])
    row = {
        "question_id": q["question_id"],
        "db_id": q["db_id"],
        "difficulty": q["difficulty"],
        **asdict(cfg),
    }
    start = time.perf_counter()
    try:
        gen = await PIPELINES[cfg.pipeline](llm, path, q["question"], q["evidence"])
    except Exception as e:  # 记录为失败，不中断整个评测
        return {
            **row,
            "pred_sql": "",
            "correct": False,
            "status": "llm_error",
            "detail": f"{type(e).__name__}: {e}",
            "cost_usd": 0.0,
            "usage": {},
            "latency_s": time.perf_counter() - start,
            "llm_calls": 0,
        }
    gen_latency = time.perf_counter() - start
    usage_keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    usage = {k: sum(c.usage.get(k, 0) for c in gen.llm_calls) for k in usage_keys}
    if gen.stop_reason == "refusal":
        status, correct, detail = "refusal", False, ""
    elif not gen.sql:
        status, correct, detail = "no_sql", False, gen.stop_reason
    else:
        out = await asyncio.to_thread(evaluate_one, path, gen.sql, q["SQL"])
        status, correct, detail = out.status, out.correct, out.detail
    return {
        **row,
        "pred_sql": gen.sql,
        "correct": correct,
        "status": status,
        "detail": detail,
        "stop_reason": gen.stop_reason,
        "cost_usd": sum(c.cost_usd for c in gen.llm_calls),
        "usage": usage,
        "latency_s": gen_latency,
        "llm_calls": len(gen.llm_calls),
    }


async def run(
    questions: list[dict],
    configs: list[Config],
    llm_factory: Callable[[Config, dict], LLM],
    run_dir: Path,
    concurrency: int,
    max_cost: float | None = None,
) -> list[dict]:
    """max_cost：本次累计花费达到上限后不再发起新调用（已在途的请求会跑完，可能略超）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    done = load_done(run_dir)
    jobs = [
        (q, c)
        for c in configs
        for q in questions
        if (q["question_id"], c.model, c.pipeline, c.effort, c.repeat) not in done
    ]
    if done:
        print(f"续跑：已完成 {len(done)}，本次 {len(jobs)}")
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    finished = 0
    spent = 0.0

    async def one(q, cfg):
        nonlocal finished, spent
        async with sem:
            if max_cost is not None and spent >= max_cost:
                return None
            r = await run_one(q, cfg, llm_factory(cfg, q))
        async with lock:
            spent += r["cost_usd"]
            with (run_dir / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            finished += 1
            mark = "✓" if r["correct"] else f"✗ {r['status']}"
            print(
                f"[{finished}/{len(jobs)}] {cfg.model} r{cfg.repeat} q{q['question_id']} {mark}",
                flush=True,
            )
        return r

    rows = [r for r in await asyncio.gather(*(one(q, c) for q, c in jobs)) if r is not None]
    if len(rows) < len(jobs):
        print(f"达到花费上限 ${max_cost:.2f}，跳过 {len(jobs) - len(rows)} 题，可用 --resume 续跑")
    print(f"本次实际花费 ${spent:.4f}")
    return rows


def estimate(questions: list[dict], configs: list[Config]) -> float:
    total = 0.0
    for cfg in configs:
        if cfg.model not in config.MODELS:
            continue
        inp, out, _ = config.PRICING[config.MODELS[cfg.model]]
        for q in questions:
            in_tok = (
                len(build_prompt(config.db_path(q["db_id"]), q["question"], q["evidence"])) / 3.5
            )
            total += (in_tok * inp + EST_OUTPUT_TOKENS * out) / 1e6
    return total


def make_factory(mock: bool):
    if mock:
        return lambda cfg, q: ScriptedLLM([SQLAnswer(sql=q["SQL"])], model="gold")
    import anthropic

    from datapilot.llm import ClaudeLLM

    client = anthropic.AsyncAnthropic(max_retries=6)
    return lambda cfg, q: ClaudeLLM(config.MODELS[cfg.model], effort=cfg.effort, client=client)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--models", default="haiku", help="逗号分隔：haiku,sonnet,opus；gold 为标准答案自检"
    )
    p.add_argument("--pipeline", default="baseline", choices=sorted(PIPELINES))
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--sample", type=int, help="按难度分层抽样题数（固定种子）")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ids", help="逗号分隔的 question_id")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--tag", default="adhoc")
    p.add_argument("--resume", help="续跑的 run_id")
    p.add_argument("--max-cost", type=float, help="本次花费硬上限（美元），达到后停止发起新调用")
    p.add_argument("--yes", action="store_true", help="确认花费，真正调用 API")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    questions = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))
    if args.ids:
        ids = {int(i) for i in args.ids.split(",")}
        questions = [q for q in questions if q["question_id"] in ids]
    if args.sample:
        questions = stratified_sample(questions, args.sample, args.seed)
    models = args.models.split(",")
    mock = models == ["gold"]
    configs = [
        Config(m, args.pipeline, args.effort, r)
        for m, r in itertools.product(models, range(args.repeats))
    ]

    run_id = args.resume or f"{args.tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    run_dir = config.RUNS_DIR / run_id
    done = load_done(run_dir)
    todo = [
        q
        for q in questions
        if any(
            (q["question_id"], c.model, c.pipeline, c.effort, c.repeat) not in done for c in configs
        )
    ]
    est = 0.0 if mock else estimate(todo, configs)
    print(f"{len(questions)} 题 × {len(configs)} 组配置，预估花费约 ${est:.2f}（未计缓存，偏保守）")
    if est > 0 and not args.yes:
        print("加 --yes 确认后执行。")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps({**vars(args), "question_ids": [q["question_id"] for q in questions]}, indent=2)
    )
    asyncio.run(
        run(questions, configs, make_factory(mock), run_dir, args.concurrency, args.max_cost)
    )

    from evals.report import load_rows

    summary = summarize(load_rows(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nrun_id={run_id}\n{render(summary)}")


if __name__ == "__main__":
    main()
