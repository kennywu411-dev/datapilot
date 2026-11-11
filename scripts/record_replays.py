"""录制问数服务的真实运行过程，生成静态回放页（demo/）用的数据。

直接调用 ChatService.ask（和 /api/ask 走同一段代码），逐事件记下到达时间；模型调用外面包一层，
记下每次调用的耗时、token 和花费。不改流程逻辑，记下来的就是页面上展示的全部内容。

用法：uv run python scripts/record_replays.py    # 读 demo/scenarios.json，写 demo/replays.json
      uv run python scripts/record_replays.py --only attr-real,attr-inject   # 只重录这几个
场景：{"id", "model", "db_id", "role", "turns"}；turns 的元素是问题，或 {"q": 问题, "session": "b"}
（换一个会话提问，用来录缓存命中）。归因场景可以带 "inject": 评测集里的例子 id，
录制前先把库复制一份、按 evals/attribution/cases.json 注入那个已知原因，录完删掉副本。
会调用模型接口，15 个场景合计约 0.12 美元。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from datapilot import config, rls, tools
from datapilot.llm import ClaudeLLM, OpenAICompatLLM
from datapilot.service import ChatService
from datapilot.store import MemoryStore

DEMO_DIR = config.ROOT / "demo"
MAX_ROWS = 50  # 结果表最多保留的行数
ATTR_ROWS = 12  # 归因：每条 SQL 的结果、每个维度的分组最多保留的行数


class RecordingLLM:
    """包一层：记录每次调用的耗时、用量、花费和发给模型的最后一条 user 消息。"""

    def __init__(self, inner, log: list[dict]):
        self.inner = inner
        self.model = inner.model
        self.log = log

    async def complete(self, system, messages, tools, output_format=None):
        start = time.perf_counter()
        res = await self.inner.complete(system, messages, tools, output_format)
        last = messages[-1]["content"]
        self.log.append(
            {
                # plan = 归因的"问题 → 指标和时间段"；sql = 生成 / 修正 SQL；answer = 写回答
                "kind": (
                    "answer"
                    if output_format is None
                    else "plan"
                    if output_format.__name__ == "AttributionPlan"
                    else "sql"
                ),
                "latency_s": round(time.perf_counter() - start, 3),
                "cost_usd": round(res.cost_usd, 6),
                "usage": res.usage,
                "user": last if isinstance(last, str) else "",
            }
        )
        return res


def make_llm(model: str):
    if model in config.API_MODELS:
        spec = config.API_MODELS[model]
        return OpenAICompatLLM(
            config.model_id(model),
            spec["base_url"],
            os.environ[spec["key_env"]],
            extra_body=spec["extra_body"],
        )
    return ClaudeLLM(config.MODELS[model])


def schema_of(db_id: str, role: str | None) -> list[dict]:
    """交给模型的表结构（按角色）：只留表名、列名、类型、说明，不带样例行。"""
    out = []
    for t in tools.list_tables(db_id):
        d = tools.describe_table(db_id, t["table"], role)
        out.append(
            {
                "table": d["table"],
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["type"],
                        "note": c["value_description"] or c["description"],
                        "restricted": bool(c.get("restricted")),
                    }
                    for c in d["columns"]
                ],
            }
        )
    return out


def slim(event: dict) -> dict:
    """事件原样保留，只裁掉页面用不到的字段：指标定义留口径相关的几项，结果表限制行数。"""
    name, data = event["event"], event["data"]
    if name == "metrics":
        data = {
            "items": [
                {
                    "name": i["name"],
                    "description": i["description"],
                    "expr": i.get("expr", ""),
                    "filters": [{"name": f["name"], "sql": f["sql"]} for f in i.get("filters", [])],
                    "matched_text": i.get("matched_text", ""),
                    "restricted": i.get("restricted", []),
                }
                for i in data["items"]
            ]
        }
    elif name == "table":
        data = {**data, "rows": data["rows"][:MAX_ROWS], "total_rows": len(data["rows"])}
    elif name == "attribution":
        data = {
            **data,
            "evidence": [
                {**e, "rows": e["rows"][:ATTR_ROWS], "total_rows": len(e["rows"])}
                for e in data["evidence"]
            ],
            "breakdowns": [
                {**b, "segments": b["segments"][:ATTR_ROWS], "total_segments": len(b["segments"])}
                for b in data["breakdowns"]
            ],
            "drill": [
                {**b, "segments": b["segments"][:ATTR_ROWS], "total_segments": len(b["segments"])}
                for b in data["drill"]
            ],
        }
        for b in [*data["breakdowns"], *data["drill"]]:
            for seg in b["segments"]:
                seg.pop("parts", None)
    return {"t": event["t"], "event": name, "data": data}


def inject(case_id: str) -> Path:
    """复制一份库并注入评测集里的一个例子，返回放副本的目录。"""
    from evals.attribution_eval import CASES, prepare_db

    case = next(c for c in json.loads(CASES.read_text(encoding="utf-8")) if c["id"] == case_id)
    return prepare_db(case)


async def record(scn: dict) -> dict:
    if scn.get("inject"):
        root = inject(scn["inject"])
        config.DB_DIR = root
        try:
            return await record({k: v for k, v in scn.items() if k != "inject"}) | {
                "inject": scn["inject"]
            }
        finally:
            config.DB_DIR = config.DATA_DIR / "dev_databases"
            shutil.rmtree(root, ignore_errors=True)
    log: list[dict] = []
    model = scn.get("model", "sonnet")
    svc = ChatService(
        MemoryStore(), model=model, llm_factory=lambda: RecordingLLM(make_llm(model), log)
    )
    role = scn.get("role")
    policy = rls.policy_for(scn["db_id"], role)
    turns = []
    for turn in scn["turns"]:
        q, session = (turn, "a") if isinstance(turn, str) else (turn["q"], turn["session"])
        n0 = len(log)
        start = time.perf_counter()
        events = []
        async for e in svc.ask(
            f"{scn['id']}-{session}", scn["db_id"], q, user_id="demo", role=role
        ):
            events.append(slim({"t": round(time.perf_counter() - start, 4), **e}))
        turns.append({"question": q, "session": session, "events": events, "llm_calls": log[n0:]})
    return {
        "id": scn["id"],
        "model": model,
        "model_id": config.model_id(model),
        "db_id": scn["db_id"],
        "role": role,
        "role_label": rls.roles_of(scn["db_id"]).get(role) if role else None,
        "role_note": policy.prompt_note() if policy else "",
        "turns": turns,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", default="", help="只录这些场景（逗号分隔），其余保留已有记录")
    ap.add_argument("--dir", type=Path, default=DEMO_DIR, help="scenarios.json 所在目录")
    args = ap.parse_args()
    load_dotenv(config.ROOT / ".env")
    sys.path.insert(0, str(config.ROOT))
    scenarios = json.loads((args.dir / "scenarios.json").read_text(encoding="utf-8"))
    path = args.dir / "replays.json"
    only = set(filter(None, args.only.split(",")))
    old = json.loads(path.read_text(encoding="utf-8")) if only and path.exists() else {}
    kept = {s["id"]: s for s in old.get("scenarios", [])}
    out = {"schemas": dict(old.get("schemas", {})), "scenarios": []}
    for scn in scenarios:
        if only and scn["id"] not in only:
            if scn["id"] in kept:
                out["scenarios"].append(kept[scn["id"]])
            continue
        rec = await record(scn)
        out["scenarios"].append(rec)
        key = f"{scn['db_id']}:{scn.get('role') or '-'}"
        if key not in out["schemas"]:
            out["schemas"][key] = schema_of(scn["db_id"], scn.get("role"))
        for t in rec["turns"]:
            print(rec["id"], "|", t["question"], "|", t["events"][-1]["data"], file=sys.stderr)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {path}")


if __name__ == "__main__":
    asyncio.run(main())
