"""
模块 4c：基于回测数据的策略评估与投资研究建议
================================================================
定位（重要）：
  本模块的输出对象是「策略参数组合 + 指定历史区间 + 指定基准」的**历史模拟结果**，
  不是对任何证券的投资建议，不包含买入/卖出/持有意见，不作行情预测或估值判断。
  所有结论仅来自本次回测已产出的数值，由固定阈值规则生成，可逐条复核。

设计要点映射（沿用《TradingAgents 可借鉴设计要点》）：
  P0-4 哨兵而非默认值   -> 指标为 None 时标注"证据不足"并扣分，绝不按 0 参与评分
  P0-5 显式缺失标记     -> 样本/交易/数据不足时直接落到 insufficient 档，不形成倾向性结论
  P0-6 结构化输出       -> verdict / score / highlights / actions / risk_flags 具名字段
  3.6  评估定位克制     -> 只陈述历史计算结果与研究流程建议
  3.7  报告即观测       -> generated_from 带运行签名，评分规则随报告一并输出
  0.   合规护栏         -> guardrail 对所有分档恒定存在，任何分数都不导向实盘或买卖
"""
from __future__ import annotations

import math
from typing import Any

from .backtest import BacktestResult
from .contracts import RunContext, Sentinel
from .reporting import DISCLAIMER

CORE = ("cumulative_return", "annualized_return", "max_drawdown", "sharpe_ratio")

GUARDRAIL = (
    "共同前提：以下评估只针对「本策略参数组合 + 指定历史区间 + 指定基准」的模拟结果，"
    "不是对任何证券的投资建议，不包含买入、卖出或持有的操作意见，也不对未来行情作预测。"
    "无论评分高低，均不得直接用于实盘交易或作为买卖依据。"
)

VERDICTS = {
    "insufficient": {
        "label": "证据不足 · 不作结论",
        "summary": "本次运行的样本长度、交易次数或指标完整性不足，规则引擎不形成任何倾向性结论。"
                   "请先补齐数据、延长区间或调整参数后重新运行。",
    },
    "caution": {
        "label": "需重构 · 反面样本",
        "summary": "该参数组合在本区间的历史模拟结果，在收益、风险或相对基准维度上明显偏弱。"
                   "研究上宜把它作为反面样本，优先排查信号逻辑与参数设定，而不是继续外推这段结果。",
    },
    "neutral": {
        "label": "仅作研究 · 待样本外验证",
        "summary": "历史模拟结果未显示出稳定且可外推的优势。建议停留在研究阶段，"
                   "先完成样本外检验与参数敏感性检验，再决定是否进入纸面跟踪。",
    },
    "constructive": {
        "label": "可进入模拟验证 · 非实盘建议",
        "summary": "历史模拟结果在所选区间与基准下相对稳健，但这仍是样本内结果。"
                   "下一步应做样本外验证与更真实的执行建模；本档同样不构成实盘或买卖依据。",
    },
}

DIMENSION_WEIGHT = {
    "return": ("绝对收益", 25),
    "risk": ("风险调整", 25),
    "relative": ("相对基准", 25),
    "robustness": ("证据充分性", 15),
    "data": ("数据质量", 10),
}


# ---------------------------------------------------------------- 小工具
def _f(v: Any) -> float | None:
    """None / NaN / 非数值一律返回 None，绝不静默变成 0（P0-4）。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pct(v: Any, digits: int = 2) -> str:
    x = _f(v)
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def _num(v: Any, digits: int = 2) -> str:
    x = _f(v)
    return "—" if x is None else f"{x:+.{digits}f}"


def _ge(v: float, pairs: list[tuple[float, float]], default: float) -> float:
    """越大越好： thresholds 降序。"""
    for threshold, score in pairs:
        if v >= threshold:
            return score
    return default


def _le(v: float, pairs: list[tuple[float, float]], default: float) -> float:
    """越小越好： thresholds 升序。"""
    for threshold, score in pairs:
        if v <= threshold:
            return score
    return default


def _level(score: float, maximum: float, unknown: bool = False) -> str:
    if unknown:
        return "unknown"
    ratio = score / maximum if maximum else 0
    return "good" if ratio >= 0.7 else ("mid" if ratio >= 0.4 else "weak")


def _dim(key: str, score: float, evidence: str, advice: str,
         unknown: bool = False) -> dict[str, Any]:
    label, maximum = DIMENSION_WEIGHT[key]
    return {
        "key": key, "label": label,
        "score": round(float(score), 1), "max": maximum,
        "level": _level(score, maximum, unknown),
        "evidence": evidence, "advice": advice,
    }


# ---------------------------------------------------------------- 五个维度
def _score_return(m: dict, ctx: RunContext, missing: list[str]) -> dict[str, Any]:
    total, annual = _f(m.get("cumulative_return")), _f(m.get("annualized_return"))
    if total is None or annual is None:
        missing.append("绝对收益维度：年化或累计收益率不可用")
        return _dim("return", 6.0,
                    "年化/累计收益率由指标层返回 null（未按 0 填充），本维度按证据不足处理。",
                    "先确认净值序列长度与有效性，再重新运行回测。", unknown=True)
    score = _ge(annual, [(0.15, 25), (0.08, 20), (ctx.rf, 14), (0.0, 9), (-0.10, 5)], 1)
    evidence = (f"累计收益率 {_pct(total)}，年化收益率 {_pct(annual)}；"
                f"对比无风险利率 {_pct(ctx.rf)}。")
    advice = ("绝对收益达到规则中的较高档位，但仍需确认它并非由单一区间或少数几笔交易贡献。"
              if score >= 14 else
              "绝对收益未稳定超过无风险利率，优先复核信号逻辑与仓位规则，而非调高阈值追求曲线。")
    return _dim("return", score, evidence, advice)


def _score_risk(m: dict, missing: list[str]) -> dict[str, Any]:
    sharpe, dd = _f(m.get("sharpe_ratio")), _f(m.get("max_drawdown"))
    vol, calmar = _f(m.get("annualized_volatility")), _f(m.get("calmar_ratio"))
    score = 0.0
    unknown = False
    if sharpe is None:
        score += 3.0
        unknown = True
        missing.append("风险调整维度：夏普比率为 null（日收益波动为 0 或样本不足）")
    else:
        score += _ge(sharpe, [(1.5, 15), (1.0, 12), (0.5, 8), (0.0, 5), (-0.5, 2)], 0.5)
    if dd is None:
        score += 2.0
        unknown = True
        missing.append("风险调整维度：最大回撤不可用")
    else:
        score += _le(abs(dd), [(0.10, 10), (0.20, 7), (0.30, 4), (0.45, 2)], 0.5)
    evidence = (f"夏普比率 {_num(sharpe)}，最大回撤 {_pct(dd)}，"
                f"年化波动率 {_pct(vol)}，Calmar {_num(calmar)}。")
    if unknown:
        advice = "存在不可用指标，本维度按证据不足扣分；不要用 0 代替缺失值后重算。"
    elif score >= 17:
        advice = "风险调整后表现相对均衡，可继续检验回撤是否集中在个别极端区间。"
    else:
        advice = "回撤或波动消耗了收益，建议做止损线、仓位上限与冷却期的参数敏感性网格。"
    return _dim("risk", score, evidence, advice, unknown)


def _score_relative(comparison: dict, missing: list[str]) -> dict[str, Any]:
    excess = _f(comparison.get("excess_return"))
    alpha = _f(comparison.get("annualized_alpha"))
    sharpe_diff = _f((comparison.get("sharpe_ratio") or {}).get("difference"))
    dd_diff = _f((comparison.get("max_drawdown") or {}).get("difference"))
    unknown = False
    if excess is None or alpha is None:
        unknown = True
        missing.append("相对基准维度：超额收益或年化 alpha 不可用")
        score = 6.0
    else:
        score = _ge(excess, [(0.10, 13), (0.03, 10), (0.0, 7), (-0.05, 4), (-0.15, 2)], 0.5)
        score += _ge(alpha, [(0.05, 6), (0.02, 4.5), (0.0, 3), (-0.05, 1.5)], 0.5)
    score += (_ge(sharpe_diff, [(0.5, 3), (0.0, 2)], 0.5) if sharpe_diff is not None else 1.5)
    # dd_diff = 策略回撤 - 基准回撤；为正表示策略回撤更浅
    score += (_ge(dd_diff, [(0.0, 3), (-0.05, 1.5)], 0.5) if dd_diff is not None else 1.5)
    evidence = (f"累计超额 {_pct(excess)}，年化 alpha {_pct(alpha)}，"
                f"夏普差 {_num(sharpe_diff)}，回撤差 {_pct(dd_diff)}（差值＝策略−基准）。")
    advice = ("相对基准占优，仍需换用不同基准指数复核，避免结论依赖单一基准选择。"
              if (excess is not None and excess > 0) else
              "相对基准落后时，应先区分「策略逻辑失效」与「本区间风格不匹配」；"
              "无论何种原因，都不构成对标的本身的评价。")
    return _dim("relative", score, evidence, advice, unknown)


def _score_robustness(m: dict, bt: BacktestResult,
                      missing: list[str]) -> dict[str, Any]:
    days = _f(m.get("trading_days")) or 0
    n_trades = len(bt.trades)
    score = _ge(float(days), [(500, 6), (250, 4.5), (120, 3), (60, 1.5)], 0.5)
    if 10 <= n_trades <= 120:
        score += 4
    elif 3 <= n_trades < 10 or 120 < n_trades <= 250:
        score += 2.5
    else:
        score += 1
    review_ratio = bt.review_no_action_days / max(days, 1)
    score += _le(review_ratio, [(0.02, 3), (0.10, 2)], 1)
    aligned = len(bt.aligned_out_dates or [])
    score += 2 if aligned == 0 else (1 if aligned <= 5 else 0.5)
    evidence = (f"有效净值 {int(days)} 个交易日，{n_trades} 笔成交，"
                f"REVIEW 不动作 {bt.review_no_action_days} 天（占比 {review_ratio * 100:.1f}%），"
                f"止损 {bt.force_sell_count} 次，基准未对齐被剔除 {aligned} 天，"
                f"期末仓位 {bt.final_position * 100:.0f}%。")
    if days < 120 or n_trades < 3:
        missing.append("证据充分性：样本交易日不足 120 天或成交少于 3 笔")
        advice = "样本或成交次数太少，统计意义上不足以评估；应延长区间后再看结论是否稳定。"
    else:
        advice = "把区间切成互不重叠的子段分别复跑，检查结论在不同子区间是否一致。"
    return _dim("robustness", score, evidence, advice)


def _score_data(dq: dict, notes: list[str]) -> dict[str, Any]:
    stock, bench = dq.get("stock", {}), dq.get("benchmark", {})
    both_ok = (stock.get("status") == Sentinel.OK and bench.get("status") == Sentinel.OK)
    score = 4.0 if both_ok else 1.0
    cov_s, cov_b = stock.get("coverage", {}), bench.get("coverage", {})
    has_cov = bool(cov_s.get("rows")) and bool(cov_b.get("rows")) and \
              bool(cov_s.get("first_date")) and bool(cov_b.get("first_date"))
    score += 3.0 if has_cov else 1.0
    score += 3.0 if len(notes) <= 3 else (2.0 if len(notes) <= 8 else 1.0)
    dropped = int(cov_s.get("dropped_invalid_rows", 0)) + int(cov_b.get("dropped_invalid_rows", 0))
    evidence = (f"个股源 {stock.get('vendor', 'N/A')}（{stock.get('status', 'N/A')}），"
                f"基准源 {bench.get('vendor', 'N/A')}（{bench.get('status', 'N/A')}）；"
                f"降级/执行留痕 {len(notes)} 条，剔除无效行情 {dropped} 行，"
                f"数据覆盖个股 {cov_s.get('first_date', '—')}~{cov_s.get('last_date', '—')}。")
    advice = ("数据链路完整，仍需留意「基准为指数归一化、不含交易成本」带来的口径差异。"
              if both_ok else "数据源状态异常，请先修复数据链路再解读任何绩效数值。")
    return _dim("data", score, evidence, advice)


# ---------------------------------------------------------------- 结论 / 发现 / 动作
def _highlights(m: dict, b: dict, bh: dict, comparison: dict,
                bt: BacktestResult, ctx: RunContext) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    excess = _f(comparison.get("excess_return"))
    out.append({
        "tone": "neu",
        "text": (f"策略累计 {_pct(m.get('cumulative_return'))}、年化 {_pct(m.get('annualized_return'))}；"
                 f"基准 {ctx.benchmark_name} 累计 {_pct(b.get('cumulative_return'))}；"
                 f"累计超额 {_pct(excess)}。"),
    })
    out.append({
        "tone": "neg" if (_f(m.get("max_drawdown")) or 0) < -0.20 else "neu",
        "text": (f"策略最大回撤 {_pct(m.get('max_drawdown'))}（基准 {_pct(b.get('max_drawdown'))}），"
                 f"夏普 {_num(m.get('sharpe_ratio'))}（基准 {_num(b.get('sharpe_ratio'))}）；"
                 f"回撤与波动需与收益一起看，单项数值不构成优劣判断。"),
    })
    out.append({
        "tone": "neu",
        "text": (f"同一标的在同一区间的买入持有累计 {_pct(bh.get('cumulative_return'))}，"
                 f"策略共 {len(bt.trades)} 笔成交、止损 {bt.force_sell_count} 次。"
                 "策略与买入持有的差异来自择时与仓位规则，不是标的本身的评价。"),
    })
    if _f(m.get("annualized_return")) is not None and _f(m.get("sharpe_ratio")) is None:
        out.append({"tone": "neg", "text": "夏普比率不可用（日收益波动为 0 或样本不足），报告以 null 呈现，未按 0 填充。"})
    notes = m.get("note") or ""
    if notes:
        out.append({"tone": "neu", "text": f"指标层附注：{notes}"})
    return out[:6]


def _actions(m: dict, comparison: dict, bt: BacktestResult,
             days: float, notes: list[str]) -> list[dict[str, str]]:
    acts: list[dict[str, str]] = []

    def add(priority: str, title: str, detail: str) -> None:
        acts.append({"priority": priority, "title": title, "detail": detail})

    if days < 250 or len(bt.trades) < 5:
        add("高", "先补齐样本，再谈结论",
            "把区间延长到至少 1 个完整年度，并预留一段完全未参与调参的样本外区间；"
            "当前交易日数或成交笔数偏少，任何评分都不稳健。")
    excess = _f(comparison.get("excess_return"))
    if excess is not None and excess < 0:
        add("高", "拆解跑输基准的原因",
            "分别对照买入持有与另一个风格不同的基准指数复跑，区分「信号逻辑失效」与「区间风格不匹配」；"
            "单一区间跑输既不能证明策略失效，也不能推导标的强弱。")
    dd = _f(m.get("max_drawdown"))
    if dd is not None and abs(dd) > 0.20:
        add("高", "做回撤口径与参数敏感性检验",
            f"当前最大回撤 {_pct(dd)}。对止损线、最大仓位、冷却交易日做网格复跑，"
            "观察结论是否只在个别参数组合上成立；同时检查回撤是否集中在少数极端交易日。")
    sharpe = _f(m.get("sharpe_ratio"))
    if sharpe is None or sharpe < 0.5:
        add("中", "复核风险收益比",
            "夏普偏低或不可用时，先看日收益分布与波动来源，再决定是否调整信号阈值；"
            "不建议用提高仓位的方式去放大本区间的收益数字。")
    add("中", "升级执行模型后复跑",
        "当前模型为次日开盘成交 + 固定费率，未含滑点、涨跌停、停牌与流动性约束。"
        "补上这些约束后再比较结论是否保持一致，是判断结果能否外推的必要一步。")
    add("中", "多标的、多区间、多基准交叉验证",
        "用同一套参数在若干不同标的与互不重叠的时间段上复跑，"
        "避免把单一标的、单一区间的偶然结果当成策略规律。")
    if len(notes) > 3:
        add("中", "先处理数据链路留痕",
            f"本次有 {len(notes)} 条数据降级/执行说明，请在「数据降级与执行说明」中逐条核对，"
            "确认没有覆盖缺口影响净值序列连续性。")
    add("常规", "把结论限定在研究范围内",
        "纸面跟踪（无真实资金）是下一步的合适验证形式；本模块不产生任何买卖操作意见，"
        "也不提供择时或持仓指令，不对行情作预测。")
    return acts[:7]


def _risk_flags(m: dict, bt: BacktestResult, insufficient: bool) -> list[str]:
    flags = [
        "样本内回测存在过拟合风险：在本区间表现好的参数，样本外可能完全失效。",
        "简化执行模型未计滑点、涨跌停、停牌与流动性约束，与真实成交可能存在显著差异。",
        "单一标的、单一区间、单一基准，结论不可外推到其他证券或市场阶段。",
        "策略跑赢或跑输基准，反映的是规则与区间的匹配程度，不构成对该标的的评价或预测。",
        "四项必需指标均为历史净值序列的计算结果；夏普比率以固定无风险利率计算，口径变化会改变数值。",
    ]
    if insufficient:
        flags.insert(0, "本次样本/指标不完整，规则引擎已落到「证据不足」档，不形成倾向性结论。")
    if bt.review_no_action_days:
        flags.append(f"区间内有 {bt.review_no_action_days} 个 REVIEW 不动作日，按 no_new_action 处理，不等同于 HOLD。")
    return flags


# ---------------------------------------------------------------- 主入口
def build_advisory(
    ctx: RunContext,
    metrics: dict[str, Any],
    comparison: dict[str, Any],
    bt: BacktestResult,
    signal_summary: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """纯函数：只读取本次回测已产出的结构化字段，不访问网络、不引入外部观点。"""
    m, b, bh = metrics["strategy"], metrics["benchmark"], metrics["buy_hold"]
    dq = state.get("data_quality", {})
    notes = list(state.get("degradation_notes", [])) + list(bt.execution_notes)
    missing: list[str] = []

    dims = [
        _score_return(m, ctx, missing),
        _score_risk(m, missing),
        _score_relative(comparison, missing),
        _score_robustness(m, bt, missing),
        _score_data(dq, notes),
    ]
    total = round(sum(d["score"] for d in dims), 1)

    days = float(_f(m.get("trading_days")) or 0)
    core_missing = any(_f(m.get(k)) is None for k in CORE)
    data_bad = any(dq.get(k, {}).get("status") != Sentinel.OK for k in ("stock", "benchmark"))
    blockers: list[str] = []
    if days < 60:
        blockers.append(f"有效净值仅 {int(days)} 个交易日（门槛 60）")
    if len(bt.trades) < 2:
        blockers.append(f"仅 {len(bt.trades)} 笔成交（门槛 2 笔）")
    if core_missing:
        blockers.append("四项必需指标中存在 null（未按 0 填充）")
    if data_bad:
        blockers.append("个股或基准数据源状态非 OK")
    insufficient = bool(blockers)

    if insufficient:
        level = "insufficient"
    elif total < 42:
        level = "caution"
    elif total < 65:
        level = "neutral"
    else:
        level = "constructive"

    verdict = dict(VERDICTS[level])
    verdict["level"] = level
    verdict["score"] = None if insufficient else total
    verdict["blockers"] = blockers
    verdict["evidence_gaps"] = missing

    highlights = _highlights(m, b, bh, comparison, bt, ctx)
    if insufficient:
        highlights.insert(0, {
            "tone": "neg",
            "text": "未达到评分门槛：以下数值仅作事实陈列，规则引擎不形成综合评分与倾向性结论。",
        })

    return {
        "available": True,
        "title": "回测解读与投资研究建议",
        "scope": (f"评估对象：本策略参数组合在 {ctx.start_date.isoformat()} ~ {ctx.end_date.isoformat()} "
                  f"对 {ctx.name}（{ctx.symbol}）以 {ctx.benchmark_name} 为基准的历史模拟结果。"
                  "评估对象是策略与规则，不是该证券，也不包含任何操作意见。"),
        "verdict": verdict,
        "score": {
            "total": None if insufficient else total,
            "raw_total": total,
            "max": 100,
            "applicable": not insufficient,
            "applicability_note": (
                "样本或指标未达门槛，本次不形成综合评分；下方维度分仅作证据明细，不得当作结论。"
                if insufficient else
                "综合评分为五维求和，仅用于归纳本次回测证据，不代表未来表现。"),
            "dimensions": dims,
            "methodology": ("五维固定阈值评分：绝对收益 25 / 风险调整 25 / 相对基准 25 / "
                            "证据充分性 15 / 数据质量 10。输入全部来自本次回测输出，"
                            "指标为 null 时按「证据不足」扣分而非按 0 计；"
                            "评分是规则化的证据归纳，不是模型预测，也不随标的名称变化。"),
        },
        "highlights": highlights,
        "actions": _actions(m, comparison, bt, days, notes),
        "risk_flags": _risk_flags(m, bt, insufficient),
        "signal_context": {
            "buy_days": signal_summary.get("buy_days", 0),
            "sell_days": signal_summary.get("sell_days", 0),
            "hold_days": signal_summary.get("hold_days", 0),
            "review_days": signal_summary.get("review_days", 0),
        },
        "guardrail": GUARDRAIL,
        "disclaimer": DISCLAIMER,
        "generated_from": {
            "run_signature": ctx.signature(),
            "as_of": ctx.as_of.isoformat(),
            "date_window": {"start": ctx.start_date.isoformat(), "end": ctx.end_date.isoformat()},
            "benchmark": ctx.benchmark,
            "benchmark_name": ctx.benchmark_name,
        },
    }
