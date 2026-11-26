"""
状态契约 / 哨兵 / 运行上下文
================================================================
本文件把《TradingAgents 可借鉴设计要点》里可移植的工程约束落到类型上：

  P0-1 角色 = 交付物契约   -> NODE_CONTRACT 节点读写表 + RunState 具名字段
  P0-2 不可信参数从调用方拿走 -> RunContext 由服务端构造，as_of 强制钳制
  P0-3 "不可用"要区分语义   -> Sentinel 四类数据哨兵 + INSTRUCTIONS 指令式错误串
  P0-4 哨兵而非默认值       -> REVIEW 不是 HOLD；NO_DATA_AVAILABLE 不是 0
  P0-6 结构化输出渐进叠加   -> dataclass / TypedDict 契约，渲染层在 reporting.py
  3.4(c) 错误按"调用方反应"分类 -> VendorErrorKind 只有 4 类
  3.7 元信息白名单           -> META_ALLOWLIST
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any, TypedDict


APP_VERSION = "1.1.0"
TRADING_DAYS = 252
DEFAULT_RF = 0.02  # 无风险利率，用于夏普比率


# ---------------------------------------------------------------- 哨兵
class Sentinel:
    """不可用/未判定必须与任何合法业务值区分开（P0-3 / P0-4）。"""

    OK = "OK"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"      # 供应商故障/限流，与标的无关
    NO_DATA_AVAILABLE = "NO_DATA_AVAILABLE"    # 确实没有数据或数据陈旧
    COVERAGE_GAP = "COVERAGE_GAP"              # 区间内存在覆盖缺口
    WITHHOLD = "WITHHOLD"                      # 无历史归档版本，历史运行刻意不给
    REVIEW = "REVIEW"                          # 未判定，禁止降级成 HOLD / 0 / 中性


INSTRUCTIONS = {
    Sentinel.DATA_UNAVAILABLE:
        "DATA_UNAVAILABLE: 数据源暂时不可用（限流/网络），与标的本身无关；"
        "不得估算或编造任何数值，请稍后重试。",
    Sentinel.NO_DATA_AVAILABLE:
        "NO_DATA_AVAILABLE: 该标的在指定窗口内没有可用行情；"
        "不得估算或编造任何数值。",
    Sentinel.COVERAGE_GAP:
        "COVERAGE_GAP: 所选区间存在数据覆盖缺口；不得用插值或估算补齐。",
    Sentinel.WITHHOLD:
        "WITHHOLD: 该数据源不提供历史归档版本，为保证 point-in-time 完整性刻意不提供。",
    Sentinel.REVIEW:
        "REVIEW: 本轮决策未判定，不等同于 HOLD，禁止下游把它当作任何业务动作。",
    "BAD_INPUT":
        "BAD_INPUT: 入参未通过服务端校验，请修正后重试；服务端不会用默认值猜测你的意图。",
}


class Decision(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    REVIEW = "REVIEW"  # 哨兵，不是业务动作


class VendorErrorKind(str, Enum):
    """错误类型数 = 调用方的不同反应数，不是人类可描述的成因数。"""

    NO_MARKET_DATA = "NoMarketDataError"           # 空/陈旧 -> 终止并报告不可用
    VENDOR_UNAVAILABLE = "VendorUnavailableError"  # 限流/超时 -> 跳下一个 vendor
    VENDOR_NOT_CONFIGURED = "VendorNotConfiguredError"  # 未配置/接口缺失 -> 跳下一个
    BAD_INPUT = "BadInputError"                    # 输入非法 -> 直接拒绝


# ---------------------------------------------------------------- 契约表
# 先画"节点 -> 读 -> 写"表，再写实现；不在表里的信息不出现在节点之间（P0-1）
NODE_CONTRACT = [
    {"module": "1 数据接入", "node": "datafeed.stock",
     "reads": ["symbol", "date_window", "as_of"], "writes": ["market_data", "data_quality.stock"]},
    {"module": "1 数据接入", "node": "datafeed.benchmark",
     "reads": ["benchmark", "date_window", "as_of"], "writes": ["benchmark_data", "data_quality.benchmark"]},
    {"module": "2 策略信号", "node": "analyst.trend",
     "reads": ["market_data"], "writes": ["trend_report"]},
    {"module": "2 策略信号", "node": "analyst.momentum",
     "reads": ["market_data"], "writes": ["momentum_report"]},
    {"module": "2 策略信号", "node": "analyst.volatility",
     "reads": ["market_data"], "writes": ["volatility_report"]},
    {"module": "2 策略信号", "node": "analyst.volume",
     "reads": ["market_data"], "writes": ["volume_report"]},
    {"module": "2 策略信号", "node": "research.manager",
     "reads": ["trend_report", "momentum_report", "volatility_report", "volume_report"],
     "writes": ["investment_plan"]},
    {"module": "2 策略信号", "node": "risk.gate",
     "reads": ["investment_plan", "market_data"], "writes": ["risk_note", "decision_series"]},
    {"module": "3 回测引擎", "node": "backtest.engine",
     "reads": ["market_data", "benchmark_data", "decision_series", "run_context"],
     "writes": ["equity_curve", "trades", "benchmark_curve", "buy_hold_curve"]},
    {"module": "4 结果输出", "node": "metrics.compute",
     "reads": ["equity_curve", "benchmark_curve"], "writes": ["metrics"]},
    {"module": "4 结果输出", "node": "advisory.build",
     "reads": ["metrics", "trades", "data_quality", "degradation_notes", "execution", "signal_summary"],
     "writes": ["advisory"]},
    {"module": "4 结果输出", "node": "reporting.build",
     "reads": ["metrics", "trades", "data_quality", "degradation_notes", "advisory"],
     "writes": ["report"]},
]

# 可写进可分享产物的元信息白名单（3.7）：端点、密钥、本地路径绝不外泄
META_ALLOWLIST = (
    "app_version", "run_signature", "as_of", "date_window", "symbol", "name",
    "benchmark", "benchmark_name", "adjust", "data_vendor", "benchmark_vendor",
    "data_coverage", "benchmark_coverage", "parameters", "generated_at",
)


# ---------------------------------------------------------------- 运行上下文
@dataclass(frozen=True)
class RunContext:
    """显式传参对象，避免全局可变状态（对应文档 5-9 改进建议）。"""

    symbol: str
    name: str
    start_date: date
    end_date: date
    as_of: date                      # 服务端注入并钳制，绝不信任前端传值
    benchmark: str = "000300"
    benchmark_name: str = "沪深300"
    adjust: str = "qfq"
    initial_capital: float = 100_000.0
    commission_rate: float = 0.00025     # 佣金 万2.5
    stamp_tax: float = 0.0005            # 卖出印花税 万5
    min_commission: float = 5.0
    max_position: float = 0.95           # 最大仓位
    stop_loss: float = 0.12              # 持仓浮亏止损线
    cooldown_days: int = 5               # 止损后冷却交易日
    fast_window: int = 20
    slow_window: int = 60
    rsi_window: int = 14
    momentum_window: int = 20
    bollinger_window: int = 20
    volume_window: int = 20
    vote_threshold: float = 0.25         # 综合得分进入 BUY/SELL 的阈值
    rf: float = DEFAULT_RF

    def signature(self) -> str:
        """运行签名：覆盖全部影响产出物的输入（对应文档 checkpoint 运行签名）。"""
        payload = asdict(self)
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def public_parameters(self) -> dict[str, Any]:
        """仅暴露允许用户复核的参数摘要。"""
        return {
            "initial_capital": self.initial_capital,
            "commission_rate": self.commission_rate,
            "stamp_tax": self.stamp_tax,
            "max_position": self.max_position,
            "stop_loss": self.stop_loss,
            "cooldown_days": self.cooldown_days,
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "rsi_window": self.rsi_window,
            "momentum_window": self.momentum_window,
            "bollinger_window": self.bollinger_window,
            "volume_window": self.volume_window,
            "vote_threshold": self.vote_threshold,
            "rf": self.rf,
        }


class RunState(TypedDict, total=False):
    """共享状态做主通道：每份交付物一个具名字段，节点不覆盖别人的 key。"""

    # 运行锚点
    symbol: str
    name: str
    trade_date: str
    as_of: str
    date_window: dict
    run_context: RunContext
    run_signature: str
    # 模块 1
    market_data: Any
    benchmark_data: Any
    data_quality: dict
    # 模块 2（四份独立报告，互不覆盖）
    trend_report: dict
    momentum_report: dict
    volatility_report: dict
    volume_report: dict
    investment_plan: dict
    risk_note: dict
    decision_series: list
    # 模块 3
    equity_curve: Any
    benchmark_curve: Any
    buy_hold_curve: Any
    trades: list
    # 模块 4
    metrics: dict
    advisory: dict
    report: dict
    # 降级与观测
    degradation_notes: list
