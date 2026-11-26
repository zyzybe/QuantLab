"""
模块 3：回测引擎
================================================================
设计要点映射：
  P0-2 / 3.4 防未来函数   -> t 日收盘生成信号，t+1 开盘价成交；绝不用当日收盘价成交
  P0-4 REVIEW 哨兵         -> REVIEW 日执行 no_new_action：跳过本次调仓、不计入业务信号、留可见标记
  4.2  结构化字段          -> decision/target_position 用枚举与数值，不解析自由文本
  3.6  评估定位克制        -> 明确这是带手续费/整手/T+1 的简化执行模型，不是真实撮合
  P0-5 显式缺失标记        -> 基准与个股交易日不对齐时只取交集，绝不前向填充编造价格

执行规则：
  * 初始资金、佣金（含最低 5 元）、卖出印花税、100 股整手
  * 每日最多调仓一次（开盘），止损在收盘判定、次日开盘强平，天然满足 T+1
  * 止损后进入冷却期，期间不再开仓
  * 基准为指数收盘价归一化（不含交易成本），买入持有为同起点同仓位规则
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .contracts import Decision, RunContext, Sentinel


@dataclass
class BacktestResult:
    dates: list[str] = field(default_factory=list)
    strategy_equity: list[float] = field(default_factory=list)
    benchmark_equity: list[float] = field(default_factory=list)
    buy_hold_equity: list[float] = field(default_factory=list)
    benchmark_close: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    review_no_action_days: int = 0
    force_sell_count: int = 0
    aligned_out_dates: list[str] = field(default_factory=list)
    execution_notes: list[str] = field(default_factory=list)
    final_position: float = 0.0
    status: str = Sentinel.OK


def _fee(amount: float, side: str, ctx: RunContext) -> float:
    commission = max(amount * ctx.commission_rate, ctx.min_commission)
    return commission + (amount * ctx.stamp_tax if side == "SELL" else 0.0)


def _buy_qty(budget: float, price: float, cash: float, ctx: RunContext) -> tuple[float, float, bool]:
    """整手优先；若一手都买不起则启用零股演示模式，并显式返回降级标记。

    A 股真实约束是 100 股整手，但高价股（如 1500 元的茅台）配合小额初始资金会导致
    整段回测 0 交易。这里不静默变成"不买"，而是降级并留可见说明。
    """
    lots = int(budget / (price * 100))
    qty = float(lots * 100) if lots > 0 else budget / price * 0.99
    fractional = lots == 0
    for _ in range(30):
        if qty <= 0:
            break
        amount = qty * price
        fee = _fee(amount, "BUY", ctx)
        if amount + fee <= cash:
            break
        qty *= 0.97
    amount = qty * price
    fee = _fee(amount, "BUY", ctx) if qty > 0 else 0.0
    return qty, fee, fractional


def run_backtest(
    full_market: pd.DataFrame,
    decision_series: list[dict],
    bench_close_map: dict[str, float],
    report_start: str,
    ctx: RunContext,
) -> BacktestResult:
    """full_market 含预热期；信号按日期索引，只在 report_start 之后执行。"""
    result = BacktestResult()
    all_dates = [str(d) for d in full_market["date"]]
    open_ = full_market["open"].astype(float).to_numpy()
    close = full_market["close"].astype(float).to_numpy()
    low = full_market["low"].astype(float).to_numpy()
    decision_map = {d["date"]: d for d in decision_series}

    # 基准与个股交易日只取交集；不对齐的日期显式记录，绝不 ffill 编造
    eligible = [i for i, d in enumerate(all_dates)
                if d >= report_start and d in bench_close_map and bench_close_map[d] > 0]
    excluded = [d for i, d in enumerate(all_dates)
                if d >= report_start and (d not in bench_close_map or bench_close_map.get(d, 0) <= 0)]
    result.aligned_out_dates = excluded
    if excluded:
        result.execution_notes.append(
            f"基准与个股交易日不对齐，剔除 {len(excluded)} 个日期（未做插值/前向填充）："
            f"{', '.join(excluded[:5])}{' ...' if len(excluded) > 5 else ''}"
        )
    if len(eligible) < 2:
        result.status = Sentinel.NO_DATA_AVAILABLE
        result.execution_notes.append("对齐后不足 2 个交易日，无法回测；不编造净值")
        return result

    first = eligible[0]
    cash = float(ctx.initial_capital)
    shares = 0
    avg_cost = 0.0
    prev_target = 0.0
    cooldown_until = -1
    force_sell = False
    base0 = float(bench_close_map[all_dates[first]])

    # 买入持有：同一天开盘、同一仓位规则、同样手续费，保证横向可比
    bh_price = float(open_[first])
    bh_shares, bh_fee, bh_fractional = _buy_qty(
        ctx.initial_capital * ctx.max_position, bh_price, ctx.initial_capital, ctx)
    bh_cash = ctx.initial_capital - bh_shares * bh_price - bh_fee
    fractional_days = 0
    if bh_fractional:
        result.execution_notes.append(
            "该标的一手金额超过可用资金，买入持有与策略均启用零股演示模式"
            "（非 A 股真实整手约束，仅用于保证高价股可回测）")

    for i in eligible:
        d = all_dates[i]
        px_open = float(open_[i])
        px_close = float(close[i])
        equity_before = cash + shares * px_open

        # ---- 开盘：执行 t-1 收盘后生成的信号（防未来函数的关键） ----
        prev_date = all_dates[i - 1] if i > 0 else None
        signal = decision_map.get(prev_date) if prev_date else None
        target = prev_target
        reason = "无前一日信号，保持既有状态"
        if force_sell:
            target, reason = 0.0, "止损强平（触发于上一交易日收盘）"
            force_sell = False
        elif signal is not None and signal.get("status") == Sentinel.REVIEW:
            # REVIEW 不是业务动作：跳过本次调仓，不改变任何状态，单独留痕
            result.review_no_action_days += 1
            reason = "REVIEW：未判定，不产生新动作（未计入业务信号统计）"
        elif signal is not None and signal.get("target_position") is not None:
            target = float(signal["target_position"])
            reason = f"前一日({prev_date})信号 {signal['decision']}，得分 {signal.get('score')}"
        if i < cooldown_until:
            target = 0.0
            reason += "；止损冷却期内不开仓"

        if target is not None and abs(target - prev_target) > 1e-9:
            desired_value = equity_before * target
            current_value = shares * px_open
            if desired_value > current_value + 1:  # 买入
                budget = min(desired_value - current_value, cash)
                qty, fee, fractional = _buy_qty(budget, px_open, cash, ctx)
                if qty > 0:
                    amount = qty * px_open
                    cash -= amount + fee
                    avg_cost = (avg_cost * shares + amount) / (shares + qty)
                    shares += qty
                    if fractional:
                        fractional_days += 1
                    result.trades.append({
                        "date": d, "side": "BUY", "price": round(px_open, 3),
                        "shares": round(qty, 2), "amount": round(amount, 2), "fee": round(fee, 2),
                        "target_position": round(target, 4), "reason": reason,
                    })
            elif current_value > desired_value + 1:  # 卖出
                qty = shares if target <= 0 else min((current_value - desired_value) / px_open, shares)
                qty = min(max(qty, 0), shares)
                if qty > 0:
                    amount = qty * px_open
                    fee = _fee(amount, "SELL", ctx)
                    cash += amount - fee
                    shares -= qty
                    if shares <= 1e-9:
                        shares, avg_cost = 0.0, 0.0
                    result.trades.append({
                        "date": d, "side": "SELL", "price": round(px_open, 3),
                        "shares": round(qty, 2), "amount": round(amount, 2), "fee": round(fee, 2),
                        "target_position": round(target, 4), "reason": reason,
                    })
        prev_target = target if target is not None else prev_target

        # ---- 收盘：结算净值、判定止损 ----
        equity = cash + shares * px_close
        result.dates.append(d)
        result.strategy_equity.append(round(float(equity), 2))
        result.benchmark_equity.append(round(float(ctx.initial_capital * bench_close_map[d] / base0), 2))
        result.benchmark_close.append(round(float(bench_close_map[d]), 4))
        result.buy_hold_equity.append(round(float(bh_cash + bh_shares * px_close), 2))

        if shares > 0 and avg_cost > 0 and (px_close / avg_cost - 1) < -ctx.stop_loss:
            force_sell = True
            cooldown_until = i + ctx.cooldown_days
            result.force_sell_count += 1
            result.execution_notes.append(f"{d} 收盘触发止损（浮亏 {(px_close / avg_cost - 1):.2%}），次日开盘强平")

    result.final_position = round(float(shares * close[eligible[-1]] /
                                        (cash + shares * close[eligible[-1]])), 4) if (cash + shares * close[eligible[-1]]) > 0 else 0.0
    if fractional_days:
        result.execution_notes.append(
            f"共 {fractional_days} 次买入因一手金额超过可用资金而启用零股演示模式")
    result.execution_notes.insert(0, (
        f"执行模型：t 日收盘信号 → t+1 开盘价成交；佣金 {ctx.commission_rate:.4%}（最低 {ctx.min_commission:.0f} 元）、"
        f"卖出印花税 {ctx.stamp_tax:.4%}、100 股整手优先（不足一手时零股兜底并标注）、同日不反向交易。"
    ))
    return result
