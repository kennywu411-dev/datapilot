"""执行准确率（EX）：与 BIRD 官方 evaluation_ex.py 对齐。

官方口径：分别执行预测 SQL 和标准 SQL，`set(pred_rows) == set(gold_rows)` 判对，
即忽略行顺序和重复行，但列顺序和取值必须一致。

超时：官方用 func_timeout 把"先执行预测、再执行标准"整体包在 30 秒里，两者共用一个预算。
所以标准 SQL 本身很慢的题（Mini-Dev 中 518 约 29 秒、701 约 158 秒）按官方口径恒判错。
这里同样共用预算，保证与官方逐题一致（runs/gold-check-*/official_crosscheck.json）。

与官方的差异（只影响安全，不影响分数）：
- 官方用读写连接执行预测 SQL；这里一律走只读连接，模型生成 DROP 也不会破坏数据库；
- 官方把执行错误和超时都记为 0 分但不区分；这里记录 status，便于失败分析。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from datapilot.sql_guard import DEFAULT_TIMEOUT_S, SQLRejected, run_readonly

Status = Literal["ok", "pred_error", "pred_rejected", "pred_timeout", "timeout", "gold_error"]


@dataclass(frozen=True)
class ExOutcome:
    correct: bool
    status: Status
    detail: str = ""


def ex_match(pred_rows: list[tuple], gold_rows: list[tuple]) -> bool:
    return set(pred_rows) == set(gold_rows)


def evaluate_one(
    db_path: str | Path,
    pred_sql: str,
    gold_sql: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ExOutcome:
    # 与官方顺序一致：先预测、后标准，共用 timeout_s
    try:
        pred = run_readonly(db_path, pred_sql, timeout_s=timeout_s, max_rows=None)
    except SQLRejected as e:
        return ExOutcome(False, "pred_rejected", str(e))
    except TimeoutError as e:
        return ExOutcome(False, "pred_timeout", str(e))
    except sqlite3.Error as e:
        return ExOutcome(False, "pred_error", str(e))

    remaining = timeout_s - pred.elapsed_s
    try:
        if remaining <= 0:
            raise TimeoutError("预测 SQL 已用完时间预算")
        gold = run_readonly(db_path, gold_sql, timeout_s=remaining, max_rows=None, skip_guard=True)
    except TimeoutError as e:
        return ExOutcome(False, "timeout", f"预测+标准共超过 {timeout_s:g} 秒：{e}")
    except sqlite3.Error as e:
        return ExOutcome(False, "gold_error", str(e))
    return ExOutcome(ex_match(pred.rows, gold.rows), "ok")
