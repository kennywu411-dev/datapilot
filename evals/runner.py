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
import os
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
from datapilot.agent import SQLAgent  # noqa: E402
from datapilot.baseline import SQLAnswer, build_prompt, generate_sql  # noqa: E402
from datapilot.ex import evaluate_one  # noqa: E402
from datapilot.llm import LLM, FatalAPIError, ScriptedLLM  # noqa: E402
from datapilot.pipeline import FixedPipeline, PipelineConfig  # noqa: E402
from datapilot.routing import RULES, LLMPair, RoutedPipeline  # noqa: E402
from evals.report import render, summarize  # noqa: E402

# 实验序列见 docs/adr/002-改进实验设计.md
PIPELINES = {
    "baseline": generate_sql,  # E0：官方提示词，schema 在 user 消息里
    "e1_cached": FixedPipeline(PipelineConfig(schema="ddl")),
    "e2_enhanced": FixedPipeline(PipelineConfig(schema="enhanced")),
    "e3_values": FixedPipeline(
        PipelineConfig(schema="enhanced", value_check=True, max_corrections=1)
    ),
    "e4_correct": FixedPipeline(
        PipelineConfig(schema="enhanced", value_check=True, max_corrections=3)
    ),
    "agent": SQLAgent(max_steps=10),
    "abl_retrieval": FixedPipeline(
        PipelineConfig(schema="enhanced", retrieval_k=3, value_check=True, max_corrections=3)
    ),
}
# 智能路由（v1.3）：E4 流程先用 Haiku，按规则升级到 Sonnet；模型名固定写 route
ROUTE_MODELS = ("haiku", "sonnet")
for _rule in RULES:
    PIPELINES[f"routed_{_rule.split('_')[0]}"] = RoutedPipeline(PIPELINES["e4_correct"], _rule)
# 每题输出 token 预估（SQL + adaptive thinking）。试跑 trial-c0b9f9 实测均值
# Haiku 255 / Sonnet 108 / Opus 123，取 2 倍余量。
EST_OUTPUT_TOKENS = 500


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
    """按难度分层抽样，各层按原比例分配名额（最大余数法，总数恰为 n），固定种子可复现。"""
    rng = random.Random(seed)
    by_diff: dict[str, list[dict]] = {}
    for q in questions:
        by_diff.setdefault(q["difficulty"], []).append(q)
    strata = sorted(by_diff.items())
    exact = [n * len(qs) / len(questions) for _, qs in strata]
    quota = [int(x) for x in exact]
    by_remainder = sorted(range(len(strata)), key=lambda i: exact[i] - quota[i], reverse=True)
    for i in by_remainder[: n - sum(quota)]:
        quota[i] += 1
    picked = []
    for (_, qs), k in zip(strata, quota, strict=True):
        picked += rng.sample(qs, k)
    return sorted(picked, key=lambda q: q["question_id"])


async def run_one(q: dict, cfg: Config, llm: LLM, role: str | None = None) -> dict:
    path = config.db_path(q["db_id"])
    row = {
        "question_id": q["question_id"],
        "db_id": q["db_id"],
        "difficulty": q["difficulty"],
        **asdict(cfg),
    }
    start = time.perf_counter()
    try:
        pipe = PIPELINES[cfg.pipeline]
        if role is None:
            gen = await pipe(llm, path, q["question"], q["evidence"])
        else:  # 行列级权限（v1.4）：只有固定流程和路由支持角色
            gen = await pipe(llm, path, q["question"], q["evidence"], role)
    except FatalAPIError:  # 密钥、欠费、额度用完：继续跑只会把剩下的题全记成失败
        raise
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
        # 记下模型最后一次的原话，排查解析失败用
        said = gen.llm_calls[-1].text[:300] if gen.llm_calls else ""
        status, correct, detail = "no_sql", False, f"{gen.stop_reason}: {said}"
    else:
        try:
            out = await asyncio.to_thread(
                evaluate_one, path, gen.sql, q["SQL"], policy=_policy(q["db_id"], role)
            )
            status, correct, detail = out.status, out.correct, out.detail
        except Exception as e:  # 评测器意外错误：记为失败并保留 SQL，不中断整次运行
            status, correct, detail = "eval_error", False, f"{type(e).__name__}: {e}"
    # 纠错改过 SQL 时，单独评测第一版，统计"改对 / 改错"
    first_correct = correct
    if gen.first_sql and gen.first_sql != gen.sql:
        try:
            first = await asyncio.to_thread(
                evaluate_one, path, gen.first_sql, q["SQL"], policy=_policy(q["db_id"], role)
            )
            first_correct = first.correct
        except Exception:
            first_correct = False
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
        "first_sql": gen.first_sql,
        "first_correct": first_correct,
        "trace": gen.trace,
    }


async def run(
    questions: list[dict],
    configs: list[Config],
    llm_factory: Callable[[Config, dict], LLM],
    run_dir: Path,
    concurrency: int,
    max_cost: float | None = None,
    role: str | None = None,
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
            r = await run_one(q, cfg, llm_factory(cfg, q), role)
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
        if cfg.model in config.LOCAL_MODELS:
            continue
        # 路由按"两个模型都跑"估上限，实际只有升级的题才调用 Sonnet
        names = ROUTE_MODELS if cfg.model == "route" else (cfg.model,)
        prices = [config.PRICING[config.model_id(n)] for n in names]
        inp, out = sum(p[0] for p in prices), sum(p[1] for p in prices)
        pipe = PIPELINES[cfg.pipeline]
        for q in questions:
            path = config.db_path(q["db_id"])
            chars = (
                pipe.input_chars(path, q["question"], q["evidence"])
                if hasattr(pipe, "input_chars")
                else len(build_prompt(path, q["question"], q["evidence"]))
            )
            in_tok = chars / 3.5
            total += (in_tok * inp + EST_OUTPUT_TOKENS * out) / 1e6
    return total


def validate(models: list[str], pipeline: str) -> None:
    """开跑前拦住不可能成功的配置：否则每题都记成 llm_error，续跑时还会被无限重试。"""
    if models == ["gold"]:
        return
    routed = pipeline.startswith("routed_")
    if routed != (models == ["route"]):
        raise SystemExit("路由流程（routed_*）只能配 --models route，route 也只能跑路由流程")
    if routed:
        return
    known = set(config.MODELS) | set(config.LOCAL_MODELS) | set(config.API_MODELS)
    unknown = [m for m in models if m not in known]
    if unknown:
        raise SystemExit(f"未知模型：{unknown}，可选：{sorted(known)}")
    if pipeline == "agent" and any(m not in config.MODELS for m in models):
        raise SystemExit("本地模型和国产 API 模型只接了固定流程，不能跑 agent 流程")
    # 没有价格就算不出花费，--max-cost 形同虚设（模型 ID 被环境变量换掉时会出现）
    unpriced = [
        config.model_id(m)
        for m in models
        if m in config.API_MODELS and config.model_id(m) not in config.PRICING
    ]
    if unpriced:
        raise SystemExit(f"{unpriced} 没有价格，先在 config.PRICING 里登记")


def _policy(db_id: str, role: str | None):
    from datapilot import rls

    return rls.policy_for(db_id, role)


def config_role_check(db_id: str, role: str) -> None:
    from datapilot import rls

    try:
        rls.policy_for(db_id, role)
    except ValueError as e:
        raise SystemExit(str(e)) from None


def check_keys(models: list[str]) -> None:
    """真正调用前确认国产模型的密钥已填：否则每题都记成 llm_error。"""
    missing = [
        config.API_MODELS[m]["key_env"]
        for m in models
        if m in config.API_MODELS and not os.environ.get(config.API_MODELS[m]["key_env"])
    ]
    if missing:
        raise SystemExit(f"缺少 {', '.join(missing)}：请在 .env 里填写")


def make_factory(mock: bool):
    if mock:

        def mock(cfg, q):
            if cfg.pipeline == "agent":
                submit = {"name": "submit_answer", "input": {"sql": q["SQL"]}}
                return ScriptedLLM([submit], model="gold")
            # 纠错流程可能追问，多备几份同样的答案；返回同一 SQL 时流程会结束
            gold = [SQLAnswer(sql=q["SQL"])] * 4
            if cfg.pipeline.startswith("routed_"):
                return LLMPair(ScriptedLLM(gold, model="gold"), ScriptedLLM(gold, model="gold"))
            return ScriptedLLM(gold, model="gold")

        return mock
    import anthropic
    import httpx2

    from datapilot.llm import ClaudeLLM, LocalLLM, OpenAICompatLLM

    client = anthropic.AsyncAnthropic(max_retries=6)
    local_client = httpx2.AsyncClient(timeout=600)  # 与 Claude 一样共用一个客户端，不每题新建
    api_client = httpx2.AsyncClient(timeout=300)

    def factory(cfg, q):
        if cfg.model == "route":
            cheap, strong = (
                ClaudeLLM(config.MODELS[m], effort=cfg.effort, client=client) for m in ROUTE_MODELS
            )
            return LLMPair(cheap, strong)
        if cfg.model in config.LOCAL_MODELS:
            return LocalLLM(config.LOCAL_MODELS[cfg.model], client=local_client)
        if cfg.model in config.API_MODELS:
            spec = config.API_MODELS[cfg.model]
            return OpenAICompatLLM(
                config.model_id(cfg.model),
                base_url=spec["base_url"],
                api_key=os.environ[spec["key_env"]],
                client=api_client,
                extra_body=spec["extra_body"],
            )
        return ClaudeLLM(config.MODELS[cfg.model], effort=cfg.effort, client=client)

    return factory


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
    p.add_argument("--questions", help="自定义题集 JSON（默认 BIRD Mini-Dev）；接入新库时用")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ids", help="逗号分隔的 question_id")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--tag", default="adhoc")
    p.add_argument("--resume", help="续跑的 run_id")
    p.add_argument("--role", help="行列级权限的角色（如 shop_ops 的 ops / sh_manager）")
    p.add_argument("--max-cost", type=float, help="本次花费硬上限（美元），达到后停止发起新调用")
    p.add_argument("--yes", action="store_true", help="确认花费，真正调用 API")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    qpath = Path(args.questions) if args.questions else config.QUESTIONS_PATH
    questions = json.loads(qpath.read_text(encoding="utf-8"))
    if args.ids:
        ids = {int(i) for i in args.ids.split(",")}
        questions = [q for q in questions if q["question_id"] in ids]
    if args.sample:
        questions = stratified_sample(questions, args.sample, args.seed)
    models = args.models.split(",")
    validate(models, args.pipeline)
    if args.role:
        pipe = PIPELINES[args.pipeline]
        base = pipe.base if isinstance(pipe, RoutedPipeline) else pipe
        if not isinstance(base, FixedPipeline) or base.cfg.schema != "enhanced":
            raise SystemExit(
                "--role 只支持增强 schema 的固定流程和路由流程（如 e4_correct、routed_r1）"
            )
        for db in {q["db_id"] for q in questions}:
            config_role_check(db, args.role)
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

    check_keys(models)
    run_dir.mkdir(parents=True, exist_ok=True)
    prev_cfg = run_dir / "config.json"
    if args.resume and prev_cfg.exists():
        prev_ids = json.loads(prev_cfg.read_text()).get("question_ids")
        if prev_ids is not None and prev_ids != [q["question_id"] for q in questions]:
            raise SystemExit(
                "续跑的题集与原运行不一致（--questions / --sample / --ids 要与原来相同）"
            )
    (run_dir / "config.json").write_text(
        json.dumps({**vars(args), "question_ids": [q["question_id"] for q in questions]}, indent=2)
    )
    try:
        asyncio.run(
            run(
                questions,
                configs,
                make_factory(mock),
                run_dir,
                args.concurrency,
                args.max_cost,
                args.role,
            )
        )
    except FatalAPIError as e:
        raise SystemExit(f"账户错误，评测已停止：{e}\n处理后用 --resume {run_id} 续跑") from None

    from evals.report import load_rows

    summary = summarize(load_rows(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nrun_id={run_id}\n{render(summary)}")


if __name__ == "__main__":
    main()
