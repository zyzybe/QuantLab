"""
模块 2：策略信号（多分析员委员会）
================================================================
设计要点映射：
  P0-1 角色 = 交付物契约   -> 每位分析员只写自己的 report_key，互不覆盖
  3.1  并行子图 + 收窄输出 -> ThreadPoolExecutor 各自独立计算，输出只含自己的序列
  2.2  并行 + barrier      -> wait() 全部完成后才进入 Research Manager
  4.2  两段辩论            -> Bull / Bear 分别汇总证据，Research Manager 裁决
  P0-4 REVIEW 哨兵         -> 数据不足/报告缺失是 REVIEW，绝不降级成 HOLD
  P0-5 显式缺失标记        -> _absent_mark()，缺席段落以文字出现而非空串
  P0-6 结构化输出          -> InvestmentPlan / DailyDecision 具名字段，渲染在 reporting.py
  5-1  结构化字段判定      -> 用 direction/status 枚举，不靠自由文本前缀
  P0-2 point-in-time      -> 指标仅用 rolling/ewm 的当日及历史值，不引用未来数据

重要：信号在 t 日收盘后生成，回测引擎在 t+1 才执行（见 backtest.py）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable

import numpy as np
import pandas as pd

from .contracts import Decision, RunContext, Sentinel

OK = Sentinel.OK
NO_DATA = Sentinel.NO_DATA_AVAILABLE
REVIEW = Sentinel.REVIEW


# ---------------------------------------------------------------- 交付物契约表
# 声明式 registry：新增分析员只要加一行数据，不改编排代码（对应 3.5 能力表思路）
ANALYST_SPECS: list[dict[str, Any]] = [
    {"key": "trend", "label": "趋势分析员", "report_key": "trend_report",
     "weight": 1.2, "fn": None},
    {"key": "momentum", "label": "动量分析员", "report_key": "momentum_report",
     "weight": 1.0, "fn": None},
    {"key": "volatility", "label": "波动率分析员", "report_key": "volatility_report",
     "weight": 0.8, "fn": None},
    {"key": "volume", "label": "量能分析员", "report_key": "volume_report",
     "weight": 0.8, "fn": None},
]


def _absent_mark(label: str) -> str:
    """P0-5：缺席段落必须是说明句，不能是空串让下游脑补。"""
    return f"(No {label} report in this run: it is not available, not an empty finding.)"


def _series_frame(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series, int]:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    open_ = df["open"].astype(float)
    n = len(df)
    return close, high, low, volume, open_, n


def _empty_entry(date_str: str, reason: str) -> dict:
    return {"date": date_str, "status": NO_DATA, "direction": None,
            "strength": 0.0, "evidence": reason}


def _build_report(key: str, label: str, entries: list[dict], note: str) -> dict:
    ok_days = sum(1 for e in entries if e["status"] == OK)
    return {
        "agent_key": key, "label": label, "report_key": f"{key}_report",
        "status": OK if ok_days > 0 else NO_DATA,
        "ok_days": ok_days, "total_days": len(entries),
        "note": note, "values": entries,   # 收窄输出：只有自己的序列
    }


# ---------------------------------------------------------------- 四位分析员
def _trend_analyst(df: pd.DataFrame, ctx: RunContext) -> dict:
    """MA 快/慢线：状态方向 + 金叉死叉事件增强。"""
    close, _, _, _, _, n = _series_frame(df)
    fast = close.rolling(ctx.fast_window, min_periods=ctx.fast_window).mean()
    slow = close.rolling(ctx.slow_window, min_periods=ctx.slow_window).mean()
    pf, ps = fast.shift(1), slow.shift(1)
    golden = (pf <= ps) & (fast > slow)
    death = (pf >= ps) & (fast < slow)
    warm = max(ctx.slow_window, ctx.fast_window)
    entries = []
    for i in range(n):
        d = str(df["date"].iloc[i])
        if i < warm or pd.isna(fast.iloc[i]) or pd.isna(slow.iloc[i]) or slow.iloc[i] == 0:
            entries.append(_empty_entry(d, _absent_mark("trend (warm-up window not filled)")))
            continue
        direction = 1 if fast.iloc[i] > slow.iloc[i] else -1
        strength = float(min(abs(fast.iloc[i] / slow.iloc[i] - 1) / 0.05, 1.0))
        event = "金叉" if golden.iloc[i] else ("死叉" if death.iloc[i] else "均线状态延续")
        if golden.iloc[i] or death.iloc[i]:
            strength = float(min(strength + 0.35, 1.0))
        entries.append({
            "date": d, "status": OK, "direction": direction, "strength": round(strength, 4),
            "evidence": f"MA{ctx.fast_window}={fast.iloc[i]:.2f} / MA{ctx.slow_window}={slow.iloc[i]:.2f}，{event}",
        })
    return _build_report("trend", "趋势分析员", entries,
                         f"双均线状态跟踪，预热 {warm} 个交易日；rolling 窗口只含当日及历史收盘价")


def _momentum_analyst(df: pd.DataFrame, ctx: RunContext) -> dict:
    """RSI 超买超卖 + N 日动量方向。"""
    close, _, _, _, _, n = _series_frame(df)
    delta = close.diff()
    alpha = 1.0 / max(ctx.rsi_window, 2)
    gain = delta.clip(lower=0).ewm(alpha=alpha, adjust=False, min_periods=ctx.rsi_window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=alpha, adjust=False, min_periods=ctx.rsi_window).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.mask(loss == 0, 100.0)                       # 只涨不跌 -> 100
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)          # 只跌不涨 -> 0
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)        # 完全横盘 -> 中性 50
    roc = close / close.shift(ctx.momentum_window) - 1
    warm = max(ctx.rsi_window + 1, ctx.momentum_window + 1)
    entries = []
    for i in range(n):
        d = str(df["date"].iloc[i])
        if i < warm or pd.isna(rsi.iloc[i]) or pd.isna(roc.iloc[i]):
            entries.append(_empty_entry(d, _absent_mark("momentum (warm-up window not filled)")))
            continue
        r = float(rsi.iloc[i])
        m = float(roc.iloc[i])
        if r > 70:
            direction, tag = -1, "RSI 超买"
        elif r < 30:
            direction, tag = 1, "RSI 超卖"
        else:
            direction = 1 if m > 0 else (-1 if m < 0 else 0)
            tag = f"{ctx.momentum_window}日动量 {'向上' if m > 0 else ('向下' if m < 0 else '走平')}"
        strength = float(min(abs(r - 50) / 50 + 0.2, 1.0))
        entries.append({
            "date": d, "status": OK, "direction": direction, "strength": round(strength, 4),
            "evidence": f"RSI{ctx.rsi_window}={r:.1f}（{tag}），ROC={m * 100:.2f}%",
        })
    return _build_report("momentum", "动量分析员", entries,
                         "Wilder 平滑 RSI + N 日收益率；超买超卖优先于动量方向")


def _volatility_analyst(df: pd.DataFrame, ctx: RunContext) -> dict:
    """布林带均值回归：偏离中轨越远，反向信号越强。"""
    close, _, _, _, _, n = _series_frame(df)
    w = ctx.bollinger_window
    mid = close.rolling(w, min_periods=w).mean()
    std = close.rolling(w, min_periods=w).std(ddof=0)
    denom = (2 * std).replace(0, np.nan)
    z = (close - mid) / denom
    entries = []
    for i in range(n):
        d = str(df["date"].iloc[i])
        if i < w or pd.isna(z.iloc[i]):
            entries.append(_empty_entry(d, _absent_mark("volatility (warm-up window not filled)")))
            continue
        zv = float(z.iloc[i])
        direction = 1 if zv < -0.4 else (-1 if zv > 0.4 else 0)
        strength = float(min(abs(zv), 1.0))
        entries.append({
            "date": d, "status": OK, "direction": direction, "strength": round(strength, 4),
            "evidence": f"布林带 z={zv:.2f}（中轨 {mid.iloc[i]:.2f}，带宽 2σ）",
        })
    return _build_report("volatility", "波动率分析员", entries,
                         f"布林带({w}, 2σ) 均值回归，|z|>0.4 触发方向")


def _volume_analyst(df: pd.DataFrame, ctx: RunContext) -> dict:
    """量能确认：放量上涨/放量下跌，缩量则降低置信度。"""
    close, _, _, volume, _, n = _series_frame(df)
    vol_s = volume.rolling(5, min_periods=5).mean()
    vol_l = volume.rolling(ctx.volume_window, min_periods=ctx.volume_window).mean()
    ratio = vol_s / vol_l.replace(0, np.nan)
    ret1 = close / close.shift(1) - 1
    warm = max(ctx.volume_window, 6)
    entries = []
    for i in range(n):
        d = str(df["date"].iloc[i])
        if i < warm or pd.isna(ratio.iloc[i]) or pd.isna(ret1.iloc[i]):
            entries.append(_empty_entry(d, _absent_mark("volume (warm-up window not filled)")))
            continue
        rv, rr = float(ratio.iloc[i]), float(ret1.iloc[i])
        direction = 1 if rr > 0 else (-1 if rr < 0 else 0)
        strength = float(min(abs(rv - 1) / 0.5, 1.0))
        if rv < 1.0:  # 缩量：方向保留但置信度打折
            strength *= 0.5
        entries.append({
            "date": d, "status": OK, "direction": direction, "strength": round(strength, 4),
            "evidence": f"量比(5/{ctx.volume_window})={rv:.2f}，当日涨跌 {rr * 100:.2f}%",
        })
    return _build_report("volume", "量能分析员", entries,
                         "短长量均线比确认价格方向，缩量减半置信度")


_ANALYST_FUNCS: dict[str, Callable[[pd.DataFrame, RunContext], dict]] = {
    "trend": _trend_analyst,
    "momentum": _momentum_analyst,
    "volatility": _volatility_analyst,
    "volume": _volume_analyst,
}
for _spec in ANALYST_SPECS:
    _spec["fn"] = _ANALYST_FUNCS[_spec["key"]]


# ---------------------------------------------------------------- 编排
def _run_analysts(df: pd.DataFrame, ctx: RunContext) -> tuple[dict[str, dict], list[str]]:
    """并行子图 + barrier：全部分析员交付完成后才进入研究阶段。"""
    notes: list[str] = []
    reports: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(ANALYST_SPECS)) as pool:
        futures = {pool.submit(spec["fn"], df, ctx): spec for spec in ANALYST_SPECS}
        done, _ = wait(futures)  # barrier
        for fut in done:
            spec = futures[fut]
            key = spec["report_key"]
            try:
                reports[key] = fut.result()  # 每位分析员只写自己的 key
            except Exception as exc:
                # 工具内异常不抛出中断流水线，改为带指令的哨兵报告（3.4(a)）
                notes.append(f"{spec['label']} 计算失败：{type(exc).__name__}: {str(exc)[:120]}；"
                             f"{_absent_mark(spec['key'])}")
                n = len(df)
                reports[key] = _build_report(
                    spec["key"], spec["label"],
                    [_empty_entry(str(df["date"].iloc[i]), _absent_mark(spec["key"]))
                     for i in range(n)],
                    "分析员不可用，本轮全部标记为 REVIEW，不编造信号",
                )
    return reports, notes


def _research_manager(reports: dict[str, dict], ctx: RunContext) -> dict:
    """两段辩论（Bull ⇄ Bear）→ 裁判产出结构化 InvestmentPlan。"""
    first = next(iter(reports.values()))
    n = first["total_days"]
    dates = [e["date"] for e in first["values"]]
    plans = []
    for i in range(n):
        available, absent = [], []
        for spec in ANALYST_SPECS:
            entry = reports[spec["report_key"]]["values"][i]
            if entry["status"] == OK:
                available.append((spec, entry))
            else:
                absent.append(_absent_mark(spec["key"]))
        if len(available) < 2:
            # 哨兵：未判定不是 HOLD，禁止下游当作业务动作
            plans.append({
                "date": dates[i], "status": REVIEW, "direction": Decision.REVIEW.value,
                "score": None, "confidence": 0.0,
                "bull_case": _absent_mark("bull evidence"),
                "bear_case": _absent_mark("bear evidence"),
                "review_reason": f"仅 {len(available)} 位分析员可用（<2），本轮未判定；" + " ".join(absent[:2]),
            })
            continue
        weighted = sum(s["weight"] * e["direction"] * e["strength"] for s, e in available)
        total_weight = sum(s["weight"] * max(e["strength"], 0.05) for s, e in available)
        score = float(weighted / total_weight) if total_weight else 0.0
        bull = [f"{s['label']}:{e['evidence']}" for s, e in available if e["direction"] > 0]
        bear = [f"{s['label']}:{e['evidence']}" for s, e in available if e["direction"] < 0]
        if score > ctx.vote_threshold:
            direction = Decision.BUY.value
        elif score < -ctx.vote_threshold:
            direction = Decision.SELL.value
        else:
            direction = Decision.HOLD.value
        plans.append({
            "date": dates[i], "status": OK, "direction": direction,
            "score": round(score, 4),
            "confidence": round(min(len(available) / len(ANALYST_SPECS), 1.0) * min(abs(score) * 2 + 0.2, 1.0), 4),
            "bull_case": "；".join(bull) if bull else _absent_mark("bull evidence"),
            "bear_case": "；".join(bear) if bear else _absent_mark("bear evidence"),
            "review_reason": "",
        })
    return {"status": OK, "debate_rounds": 1, "analyst_count": len(ANALYST_SPECS),
            "threshold": ctx.vote_threshold, "values": plans}


def _risk_gate(plan: dict, df: pd.DataFrame, ctx: RunContext) -> tuple[list[dict], dict, list[str]]:
    """风控三方（激进 / 保守 / 中立）结构化折中，产出最终交易决策序列。"""
    close = df["close"].astype(float)
    vol20 = (close / close.shift(1) - 1).rolling(20, min_periods=20).std()
    notes: list[str] = []
    decisions: list[dict] = []
    prev_target = 0.0
    reduced_days = 0
    for i, p in enumerate(plan["values"]):
        if p["status"] == REVIEW:
            # REVIEW 不映射为任何业务动作：target_position 为 None，仅留可见标记
            decisions.append({
                "date": p["date"], "decision": Decision.REVIEW.value,
                "target_position": None, "score": None, "confidence": 0.0,
                "status": REVIEW, "bull_case": p["bull_case"], "bear_case": p["bear_case"],
                "review_reason": p["review_reason"], "risk_note": "未判定，不产生新动作",
            })
            continue
        if p["direction"] == Decision.BUY.value:
            target = ctx.max_position                      # 激进派
        elif p["direction"] == Decision.SELL.value:
            target = 0.0
        else:
            target = prev_target                           # HOLD = 保持既有目标仓位
        risk_note = "激进派：按信号满仓；中立派：维持方向"
        v = vol20.iloc[i] if i < len(vol20) else np.nan
        if not pd.isna(v) and v > 0.035 and target > 0:
            target = round(target * 0.6, 4)                # 保守派：高波动降仓
            risk_note = f"保守派主张降仓（20日波动率 {v:.2%} > 3.5%）；三方折中后仓位 {target:.0%}"
            reduced_days += 1
        prev_target = target
        decisions.append({
            "date": p["date"], "decision": p["direction"],
            "target_position": round(float(target), 4),
            "score": p["score"], "confidence": p["confidence"],
            "status": OK, "bull_case": p["bull_case"], "bear_case": p["bear_case"],
            "review_reason": "", "risk_note": risk_note,
        })
    risk_note = {
        "max_position": ctx.max_position, "stop_loss": ctx.stop_loss,
        "cooldown_days": ctx.cooldown_days,
        "high_volatility_reduced_days": reduced_days,
        "note": "风控只调整仓位与约束，不改写研究裁判的方向；止损与冷却由回测引擎执行",
    }
    return decisions, risk_note, notes


def generate_signals(state: dict, ctx: RunContext) -> dict:
    """模块 2 入口：写入 state 的四份分析员报告 / 投资计划 / 决策序列。"""
    df = state["market_data"]
    reports, notes = _run_analysts(df, ctx)
    for key, report in reports.items():
        state[key] = report                      # 具名交付物，互不覆盖
    plan = _research_manager(reports, ctx)
    state["investment_plan"] = plan
    decisions, risk_note, gate_notes = _risk_gate(plan, df, ctx)
    state["decision_series"] = decisions
    state["risk_note"] = risk_note
    notes.extend(gate_notes)

    review_days = sum(1 for d in decisions if d["decision"] == Decision.REVIEW.value)
    if review_days:
        notes.append(
            f"共 {review_days} 个交易日为 REVIEW（多为指标预热期或分析员不可用），"
            f"这些日期不产生任何交易动作，也不计入业务信号统计。"
        )
    summary = {
        "total_days": len(decisions),
        "buy_days": sum(1 for d in decisions if d["decision"] == Decision.BUY.value),
        "sell_days": sum(1 for d in decisions if d["decision"] == Decision.SELL.value),
        "hold_days": sum(1 for d in decisions if d["decision"] == Decision.HOLD.value),
        "review_days": review_days,
        "analysts": [
            {"key": r["agent_key"], "label": r["label"], "status": r["status"],
             "ok_days": r["ok_days"], "note": r["note"]}
            for r in reports.values()
        ],
    }
    return {"summary": summary, "notes": notes}
