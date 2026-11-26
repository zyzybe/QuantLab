"""
模块 4a：绩效指标
================================================================
必须报告：累计收益率、年化收益率、最大回撤、夏普比率。
设计要点映射：
  P0-4 哨兵而非默认值 -> 样本不足或波动为 0 时返回 None（前端显示"—"），绝不填 0
  3.6  评估定位克制   -> 只陈述历史净值序列的计算结果，不作收益评价或预测
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .contracts import DEFAULT_RF, Sentinel, TRADING_DAYS

CORE_KEYS = ("cumulative_return", "annualized_return", "max_drawdown", "sharpe_ratio")


def compute_metrics(equity: list[float], rf: float = DEFAULT_RF) -> dict[str, Any]:
    eq = np.asarray([float(x) for x in equity], dtype=float)
    out: dict[str, Any] = {
        "status": Sentinel.OK,
        "trading_days": 0,
        "cumulative_return": None,
        "annualized_return": None,
        "max_drawdown": None,
        "sharpe_ratio": None,
        "annualized_volatility": None,
        "calmar_ratio": None,
        "note": "",
    }
    if eq.size < 2 or eq[0] <= 0 or not np.all(np.isfinite(eq)):
        out["status"] = Sentinel.NO_DATA_AVAILABLE
        out["note"] = "净值序列不足或含无效值，四项指标不可用（未以 0 填充）"
        return out

    daily = eq[1:] / eq[:-1] - 1
    n = int(daily.size)
    total = float(eq[-1] / eq[0] - 1)
    years = n / TRADING_DAYS
    annual = float((eq[-1] / eq[0]) ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else None
    peak = np.maximum.accumulate(eq)
    max_dd = float(np.min(eq / peak - 1))
    sd = float(np.std(daily, ddof=1)) if n > 1 else 0.0
    annual_vol = float(sd * math.sqrt(TRADING_DAYS)) if sd > 0 else None
    sharpe = float((np.mean(daily) - rf / TRADING_DAYS) / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else None

    out.update({
        "trading_days": n + 1,
        "cumulative_return": total,
        "annualized_return": annual,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
        "annualized_volatility": annual_vol,
        "calmar_ratio": float(annual / abs(max_dd)) if annual is not None and max_dd < 0 else None,
    })
    if years < 0.25:
        out["note"] = "回测区间不足一个季度，年化收益率与夏普比率的参考价值有限"
    if sharpe is None:
        out["note"] = (out["note"] + "；日收益波动为 0，夏普比率不可用（未以 0 填充）").strip("；")
    return out


def compare_metrics(strategy: dict, benchmark: dict) -> dict[str, Any]:
    """策略 vs 基准的横向对比：四项必需指标逐项给出差值。"""
    comparison: dict[str, Any] = {}
    for key in CORE_KEYS:
        s, b = strategy.get(key), benchmark.get(key)
        comparison[key] = {
            "strategy": s,
            "benchmark": b,
            "difference": (float(s) - float(b)) if s is not None and b is not None else None,
        }
    comparison["excess_return"] = comparison["cumulative_return"]["difference"]
    comparison["annualized_alpha"] = comparison["annualized_return"]["difference"]
    comparison["note"] = "差值为策略减基准；基准为指数收盘价归一化，不含交易成本"
    return comparison
