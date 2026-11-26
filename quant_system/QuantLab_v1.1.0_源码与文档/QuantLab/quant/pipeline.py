"""
编排层：四模块流水线
================================================================
入口层(API) 与 CLI 共用同一条 run_pipeline，保证"报告 parity"。
设计要点映射：
  6.1 最小可复制骨架 -> RunState 具名状态 / as_of 注入 / 显式降级 / 运行签名
  5-9 显式 RunContext -> 不用全局 set_config，多请求并发互不串配置
  3.7 运行签名       -> signature() 覆盖全部影响产出物的输入
  P0-3 失败语义       -> 核心数据失败返回指令式哨兵，绝不返回半截数值
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .advisory import build_advisory
from .backtest import run_backtest
from .contracts import INSTRUCTIONS, RunContext, Sentinel
from .datafeed import DEFAULT_BENCHMARK, fetch_price_series, get_benchmark, load_universe
from .metrics import compare_metrics, compute_metrics
from .reporting import build_report, error_report
from .signals import generate_signals

WARMUP_DAYS = 420          # 自然日，覆盖 60 日均线等最长预热窗口
MIN_START = date(1995, 1, 1)
MAX_SPAN_DAYS = 365 * 12


def resolve_name(symbol: str) -> str:
    """P0-2：实体身份由服务端确定性解析，不随调用方传来的名称漂移。"""
    for item in load_universe()["items"]:
        if item["code"] == symbol:
            return item["name"]
    return "名录未收录标的"


def build_context(payload: dict[str, Any]) -> tuple[RunContext | None, str | None]:
    """校验 + 钳制：所有边界参数在服务端生成，绝不信任前端。"""
    symbol = str(payload.get("symbol", "")).strip()
    if not (symbol.isdigit() and len(symbol) == 6):
        return None, "股票代码必须是 6 位数字（如 600519、000001、300750）"
    try:
        start = date.fromisoformat(str(payload.get("start_date", "")))
        end = date.fromisoformat(str(payload.get("end_date", "")))
    except (ValueError, TypeError):
        return None, "日期格式应为 YYYY-MM-DD"

    today = date.today()
    as_of = min(end, today)              # 服务端注入，前端无法拿到未来数据
    if start < MIN_START:
        return None, f"起始日期不得早于 {MIN_START.isoformat()}"
    if start >= as_of:
        return None, "起始日期必须早于结束日期，且结束日期不得晚于今天"
    if (as_of - start).days > MAX_SPAN_DAYS:
        return None, "单次回测区间不得超过 12 年"

    bench_code = str(payload.get("benchmark", DEFAULT_BENCHMARK))
    bench_meta = get_benchmark(bench_code)
    if bench_meta["code"] != bench_code:
        return None, "不支持的基准指数"

    def num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(payload.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(v, lo), hi)

    ctx = RunContext(
        symbol=symbol,
        name=resolve_name(symbol),
        start_date=start,
        end_date=as_of,
        as_of=as_of,
        benchmark=bench_meta["code"],
        benchmark_name=bench_meta["name"],
        initial_capital=num("initial_capital", 1_000_000.0, 10_000.0, 100_000_000.0),
        commission_rate=num("commission_rate", 0.00025, 0.0, 0.005),
        stamp_tax=num("stamp_tax", 0.0005, 0.0, 0.005),
        max_position=num("max_position", 0.95, 0.1, 1.0),
        stop_loss=num("stop_loss", 0.12, 0.02, 0.5),
        cooldown_days=int(num("cooldown_days", 5, 0, 30)),
        fast_window=int(num("fast_window", 20, 3, 120)),
        slow_window=int(num("slow_window", 60, 10, 250)),
        rsi_window=int(num("rsi_window", 14, 3, 60)),
        momentum_window=int(num("momentum_window", 20, 3, 120)),
        bollinger_window=int(num("bollinger_window", 20, 5, 120)),
        volume_window=int(num("volume_window", 20, 5, 120)),
        vote_threshold=num("vote_threshold", 0.25, 0.0, 1.0),
        rf=num("rf", 0.02, 0.0, 0.1),
    )
    if ctx.fast_window >= ctx.slow_window:
        return None, "快线窗口必须小于慢线窗口"
    return ctx, None


def run_pipeline(ctx: RunContext) -> dict[str, Any]:
    """四模块顺序执行，每阶段只写自己的具名交付物。"""
    state: dict[str, Any] = {
        "run_context": ctx,
        "run_signature": ctx.signature(),
        "as_of": ctx.as_of.isoformat(),
        "date_window": {"start": ctx.start_date.isoformat(), "end": ctx.end_date.isoformat()},
        "degradation_notes": [],
    }

    # ---- 模块 1：数据接入（含预热期，加热数据也受 as_of 钳制） ----
    fetch_start = ctx.start_date - timedelta(days=WARMUP_DAYS)
    stock = fetch_price_series("stock", ctx.symbol, fetch_start, ctx.end_date,
                               ctx.as_of, ctx.adjust)
    bench = fetch_price_series("benchmark", ctx.benchmark, fetch_start, ctx.end_date,
                               ctx.as_of)
    state["data_quality"] = {"stock": stock.as_quality(), "benchmark": bench.as_quality()}
    if not stock.ok:
        return error_report(stock.status,
                            f"个股 {ctx.symbol} 行情不可用：{stock.status}",
                            stock.notes + bench.notes)
    if not bench.ok:
        return error_report(bench.status,
                            f"基准 {ctx.benchmark_name} 行情不可用：{bench.status}",
                            stock.notes + bench.notes)
    state["market_data"] = stock.frame
    state["benchmark_data"] = bench.frame
    state["degradation_notes"].extend(stock.notes)
    state["degradation_notes"].extend(bench.notes)
    bench_close_map = {
        str(row["date"]): float(row["close"])
        for _, row in bench.frame.iterrows()
        if float(row["close"]) > 0
    }

    # ---- 模块 2：策略信号（并行分析员 -> 多空辩论 -> 裁判 -> 风控） ----
    signal_info = generate_signals(state, ctx)
    state["degradation_notes"].extend(signal_info["notes"])
    signal_summary = signal_info["summary"]
    signal_summary["investment_plan"] = {
        "analyst_count": state["investment_plan"]["analyst_count"],
        "debate_rounds": state["investment_plan"]["debate_rounds"],
        "vote_threshold": state["investment_plan"]["threshold"],
    }
    signal_summary["risk_note"] = state["risk_note"]

    # ---- 模块 3：回测引擎 ----
    bt = run_backtest(stock.frame, state["decision_series"], bench_close_map,
                      ctx.start_date.isoformat(), ctx)
    if bt.status != Sentinel.OK:
        return error_report(bt.status, "回测无法完成：交易日对齐后样本不足",
                            bt.execution_notes + state["degradation_notes"])

    # ---- 模块 4：结果输出 ----
    metrics = {
        "strategy": compute_metrics(bt.strategy_equity, ctx.rf),
        "benchmark": compute_metrics(bt.benchmark_equity, ctx.rf),
        "buy_hold": compute_metrics(bt.buy_hold_equity, ctx.rf),
    }
    comparison = compare_metrics(metrics["strategy"], metrics["benchmark"])
    state["equity_curve"] = bt.strategy_equity
    state["metrics"] = metrics
    # 模块 4c：只读取已产出的结构化结果，规则化生成"策略评估 + 研究建议"
    advisory = build_advisory(ctx, metrics, comparison, bt, signal_summary, state)
    state["advisory"] = advisory
    return build_report(ctx, state, bt, signal_summary, metrics, comparison, advisory)
