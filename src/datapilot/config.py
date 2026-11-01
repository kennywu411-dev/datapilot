"""全局配置：路径、模型与价格。"""

from __future__ import annotations

import os
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

# 本地开源模型（Ollama），花费记为 0
LOCAL_MODELS = {"qwen7b": "qwen2.5-coder:7b"}

# 国产 API 模型（OpenAI 兼容接口），只跑固定流程。
# 模型 ID 可用环境变量覆盖（以厂商 /models 返回为准）。
API_MODELS = {
    "deepseek": {
        "model": "deepseek-flash",  # V4 Flash 在接口里的名字（/models 返回）
        "model_env": "DEEPSEEK_MODEL",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "extra_body": {},
    },
    "qwen": {
        "model": "qwen3.7-plus",  # 试跑后选定（比旧版 qwen-plus 新一代），登记的结果都是它
        "model_env": "QWEN_MODEL",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "extra_body": {"enable_thinking": False},  # 关掉思考：思考模式输出价贵 4 倍
    },
}

# 美元 / 百万 token：(输入, 输出, 缓存读取)。缓存写入按输入价 1.25 倍计。
# 来源：Anthropic 官方价格，价格变动时只改这里。
PRICING = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    # 国产模型：第三方整理价（官方页面有出入），试跑 20 题后按控制台账单核对再改。
    # DeepSeek V4 Flash 按高峰价；通义千问按 1 美元 = 7.1 元折算
    "deepseek-flash": (0.30, 1.20, 0.03),
    "qwen-plus": (0.113, 0.282, 0.023),  # ¥0.8 / ¥2 / 缓存 ¥0.16
    "qwen3.7-plus": (0.282, 1.127, 0.056),  # ¥2 / ¥8 / 缓存 ¥0.4（官方页面标价，限时 8 折未计）
}


def model_id(name: str) -> str:
    """评测里的短名（haiku / deepseek …）→ 实际模型 ID；国产模型可用环境变量覆盖。"""
    if name in API_MODELS:
        spec = API_MODELS[name]
        return os.environ.get(spec["model_env"]) or spec["model"]
    return MODELS.get(name) or LOCAL_MODELS[name]


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
