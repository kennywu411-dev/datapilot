"""ROI 测算：实测输入 + 明确标注的假设 + 敏感性分析，输出 Markdown 表（写入 docs/roi.md）。

实测（来自运行记录）与假设（无数据，人为设定）严格分开，假设改了重跑即可。
"""

from __future__ import annotations

# 注意：净节省 / API 费用的倍数没有意义（分母可忽略），只输出净节省与盈亏平衡点。

# ---------- 实测 ----------
COST_PER_Q = 0.0046  # Sonnet 问数服务全流程单次花费（cache-bench-2f535c 未命中均值）
LATENCY_S = 3.71  # 同上，未命中 P50
EX_STRICT = 0.724  # Sonnet E4 3 次均值（严格 EX）
EX_ADJUSTED = 0.899  # 人工判定外推（估计值，见 numbers.md 失败分析）

# ---------- 假设（无实测数据）----------
T_MANUAL_MIN = 15.0  # 分析师手写并验证一条 SQL 的时间
T_REVIEW_MIN = 2.0  # 人看一遍系统给出的 SQL 和结果
T_FIX_MIN = 10.0  # 系统答错时，人发现并改对的时间
HOURLY_USD = 20.0  # 分析师人力成本（美元 / 小时）
QUESTIONS_PER_MONTH = 1000


def saved_minutes(t_manual: float, acc: float, t_review: float = T_REVIEW_MIN) -> float:
    """每题节省 = 手写时间 - (审核时间 + 答错概率 × 修正时间)。"""
    return t_manual - (t_review + (1 - acc) * T_FIX_MIN)


def monthly(t_manual: float, acc: float) -> tuple[float, float, float]:
    mins = saved_minutes(t_manual, acc)
    hours = mins * QUESTIONS_PER_MONTH / 60
    labor = hours * HOURLY_USD
    api = COST_PER_Q * QUESTIONS_PER_MONTH
    return hours, labor - api, (labor - api) / api if api else float("inf")


def main() -> None:
    lines = [
        "| 手写一题（分钟） | 准确率 | 每题节省（分钟） | 每月节省工时 | 每月净节省 |",
        "|---|---|---|---|---|",
    ]
    for t in (5, 15, 30):
        for name, acc in (("严格 EX 72.4%", EX_STRICT), ("人工判定外推 ≈90%", EX_ADJUSTED)):
            h, net, _ = monthly(t, acc)
            mins = saved_minutes(t, acc)
            lines.append(f"| {t} | {name} | {mins:.1f} | {h:.0f} 小时 | ${net:,.0f} |")
    # 盈亏平衡：手写时间多短时就不划算
    be = T_REVIEW_MIN + (1 - EX_STRICT) * T_FIX_MIN
    print("\n".join(lines))
    print(f"\n盈亏平衡（严格 EX）：手写一题 < {be:.1f} 分钟时，用系统反而更慢")


if __name__ == "__main__":
    main()
