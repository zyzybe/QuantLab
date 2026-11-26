"""
模块 4b：结果输出装配
================================================================
设计要点映射：
  3.7 元信息白名单     -> meta 只保留 META_ALLOWLIST 允许的键，端点/路径/密钥绝不写入
  P0-5 显式缺失标记    -> degradation_notes 逐条列出降级与缺口，不静默吞掉
  3.7 报告即观测       -> node_contract / signal_summary / execution_notes 一并输出
  0. 免责声明          -> 明确历史回测不代表未来表现、不构成投资建议
  4c 建议装配          -> advisory 由编排层计算后注入；失败时返回 WITHHOLD 显式哨兵
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .backtest import BacktestResult
from .contracts import (
    APP_VERSION,
    INSTRUCTIONS,
    META_ALLOWLIST,
    NODE_CONTRACT,
    RunContext,
    Sentinel,
)

DISCLAIMER = (
    "本系统为量化研究与技术演示工具。所有数值均为历史数据在同一回测规则下的计算结果，"
    "采用简化执行模型（次日开盘成交、固定费率、100 股整手优先且高价股可零股兜底），"
    "不含滑点、涨跌停、停牌与流动性约束。"
    "历史回测不代表未来表现，不构成任何投资建议。"
)


def withheld_advisory() -> dict[str, Any]:
    """回测未产出有效数值时，建议模块也必须给出显式哨兵，而不是空对象或 0 分结论。"""
    return {
        "available": False,
        "status": Sentinel.WITHHOLD,
        "title": "回测解读与投资研究建议",
        "message": "本次运行未产出有效回测结果，不生成任何评估、评分或研究建议；不得估算或编造结论。",
        "guardrail": "无有效数据时，不对任何证券给出操作意见或行情判断。",
        "disclaimer": DISCLAIMER,
    }


def build_report(
    ctx: RunContext,
    state: dict,
    bt: BacktestResult,
    signal_summary: dict,
    metrics: dict,
    comparison: dict,
    advisory: dict | None = None,
) -> dict[str, Any]:
    dq = state.get("data_quality", {})
    raw_meta = {
        "app_version": APP_VERSION,
        "run_signature": state.get("run_signature", ""),
        "as_of": ctx.as_of.isoformat(),
        "date_window": {"start": ctx.start_date.isoformat(), "end": ctx.end_date.isoformat()},
        "symbol": ctx.symbol,
        "name": ctx.name,
        "benchmark": ctx.benchmark,
        "benchmark_name": ctx.benchmark_name,
        "adjust": ctx.adjust,
        "data_vendor": dq.get("stock", {}).get("vendor", "N/A"),
        "benchmark_vendor": dq.get("benchmark", {}).get("vendor", "N/A"),
        "data_coverage": dq.get("stock", {}).get("coverage", {}),
        "benchmark_coverage": dq.get("benchmark", {}).get("coverage", {}),
        "parameters": ctx.public_parameters(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta = {k: raw_meta[k] for k in META_ALLOWLIST if k in raw_meta}

    # data_quality 里已含取数降级说明，这里只收集编排与执行层的留痕，避免重复
    notes = list(state.get("degradation_notes", []))
    notes.extend(bt.execution_notes)
    if bt.review_no_action_days:
        notes.append(
            f"回测区间内 {bt.review_no_action_days} 个交易日的信号为 REVIEW，"
            f"已按 no_new_action 处理（不调仓、不计入业务信号统计）。{INSTRUCTIONS[Sentinel.REVIEW]}"
        )

    return {
        "status": Sentinel.OK,
        "status_message": "回测完成：以下为历史数据计算结果",
        "disclaimer": DISCLAIMER,
        "meta": meta,
        "data_quality": dq,
        "degradation_notes": notes,
        "node_contract": NODE_CONTRACT,
        "curves": {
            "dates": bt.dates,
            "strategy": bt.strategy_equity,
            "benchmark": bt.benchmark_equity,
            "buy_hold": bt.buy_hold_equity,
            "benchmark_close": bt.benchmark_close,
        },
        "metrics": {
            "strategy": metrics["strategy"],
            "benchmark": metrics["benchmark"],
            "buy_hold": metrics["buy_hold"],
            "comparison": comparison,
        },
        "advisory": advisory or withheld_advisory(),
        "trades": bt.trades,
        "signal_summary": signal_summary,
        "execution": {
            "review_no_action_days": bt.review_no_action_days,
            "force_sell_count": bt.force_sell_count,
            "trade_count": len(bt.trades),
            "final_position": bt.final_position,
            "aligned_out_dates": bt.aligned_out_dates,
        },
    }


def error_report(code: str, message: str, extra: list[str] | None = None) -> dict[str, Any]:
    """失败时返回结构化哨兵响应，而不是一串 0 或半截净值曲线（P0-3 / P0-4）。"""
    return {
        "status": code,
        "status_message": message,
        "instruction": INSTRUCTIONS.get(code, "不得估算或编造任何数值"),
        "disclaimer": DISCLAIMER,
        "degradation_notes": list(extra or []),
        "curves": {"dates": [], "strategy": [], "benchmark": [], "buy_hold": [], "benchmark_close": []},
        "metrics": {},
        "advisory": withheld_advisory(),
        "trades": [],
    }
