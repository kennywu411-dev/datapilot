"""全局配置：路径、模型与价格。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_DIR = DATA_DIR / "dev_databases"
QUESTIONS_PATH = DATA_DIR / "mini_dev_sqlite.json"
RUNS_DIR = ROOT / "runs"

# 评测对比的三档模型
MODELS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
DEFAULT_MODEL = MODELS["sonnet"]

# 美元 / 百万 token：(输入, 输出, 缓存读取)。缓存写入按输入价 1.25 倍计。
# 来源：Anthropic 官方价格（2026-10 核对），价格变动时只改这里。
PRICING = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}


def db_path(db_id: str) -> Path:
    return DB_DIR / db_id / f"{db_id}.sqlite"


def cost_usd(model: str, usage: dict) -> float:
    """按 usage 计算一次调用的花费（美元）。"""
    inp, out, cache_read = PRICING.get(model, (0.0, 0.0, 0.0))
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
        + usage.get("cache_read_input_tokens", 0) * cache_read
        + usage.get("output_tokens", 0) * out
    ) / 1_000_000
