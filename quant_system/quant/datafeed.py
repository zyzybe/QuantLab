"""
模块 1：数据接入（akshare）
================================================================
设计要点映射：
  3.4(d) 配置即有序链、绝不静默回落  -> STOCK_VENDORS / BENCH_VENDORS 显式链表
  3.4(c) 类型化错误 + 指令式哨兵     -> classify_exception / INSTRUCTIONS
  P0-2  as_of 钳制、防未来函数       -> _clip_to_as_of，服务端强制，不信任前端
  P0-3  "不可用"区分语义             -> DataResult.status 返回哨兵而非空 DataFrame
  3.4(e) 原子写 + 缓存 + 运行级隔离   -> _atomic_write_csv / _Throttle / 显式传参
  5-4   数据覆盖度作为一等输出        -> DataResult.coverage

实测基线（akshare 1.20.65，2026-11-25）：
  stock_zh_a_hist / index_zh_a_hist / stock_zh_index_daily 等旧名已全部更名；
  东方财富 push2 并发会触发 Push2RateLimitError，因此东财需节流且排在链首但可撤退；
  新浪指数返回全历史（需本地裁剪），沪深交易所名录不依赖东财。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import akshare as ak
import pandas as pd

from .contracts import (
    INSTRUCTIONS,
    Sentinel,
    VendorErrorKind,
)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PRICE_CACHE_TTL = 6 * 3600
UNIVERSE_CACHE_TTL = 7 * 24 * 3600

PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


# ---------------------------------------------------------------- 基准清单
BENCHMARKS: list[dict[str, str]] = [
    {"code": "000300", "name": "沪深300", "sina": "sh000300", "tx": "sh000300", "em": "000300"},
    {"code": "000001", "name": "上证指数", "sina": "sh000001", "tx": "sh000001", "em": "000001"},
    {"code": "000905", "name": "中证500", "sina": "sh000905", "tx": "sh000905", "em": "000905"},
    {"code": "000852", "name": "中证1000", "sina": "sh000852", "tx": "sh000852", "em": "000852"},
    {"code": "399006", "name": "创业板指", "sina": "sz399006", "tx": "sz399006", "em": "399006"},
]
DEFAULT_BENCHMARK = "000300"


def get_benchmark(code: str) -> dict[str, str]:
    for item in BENCHMARKS:
        if item["code"] == code:
            return item
    return BENCHMARKS[0]


# ---------------------------------------------------------------- 节流器
class _Throttle:
    """同类数据源的最小调用间隔，避免东方财富 push2 限流。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


_THROTTLES = {
    "eastmoney": _Throttle(1.6),
    "sina": _Throttle(0.8),
    "tx": _Throttle(0.8),
    "exchange": _Throttle(0.5),
}


# ---------------------------------------------------------------- 错误分类
def classify_exception(exc: BaseException) -> VendorErrorKind:
    """按调用方应该怎么反应分类，而不是按成分类。"""
    name = type(exc).__name__
    msg = str(exc)
    if isinstance(exc, (ValueError, TypeError, KeyError)) and "数据" in msg:
        return VendorErrorKind.NO_MARKET_DATA
    if any(k in name for k in ("RateLimit", "Timeout", "Connection", "Proxy", "HTTP")):
        return VendorErrorKind.VENDOR_UNAVAILABLE
    if any(k in msg for k in ("限流", "断开", "超时", "Max retries", "timed out")):
        return VendorErrorKind.VENDOR_UNAVAILABLE
    if isinstance(exc, AttributeError) or "更名" in msg:
        return VendorErrorKind.VENDOR_NOT_CONFIGURED
    return VendorErrorKind.VENDOR_UNAVAILABLE


# ---------------------------------------------------------------- 缓存（原子写）
def _cache_file(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return CACHE_DIR / f"{safe}.csv"


def _read_cache(path: Path, ttl: int) -> pd.DataFrame | None:
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > ttl:
            return None
        return pd.read_csv(path, dtype={"date": str}, encoding="utf-8")
    except Exception:
        return None


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """os.replace 原子写；Windows 文件占用时降级保留旧缓存，不中断主流程。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_csv(tmp, index=False, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_cache_meta(path: Path, payload: dict) -> None:
    """把真实数据源、覆盖度、降级说明随缓存原子落盘，保证报告元信息不退化。"""
    meta_path = path.with_suffix(".meta.json")
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, meta_path)
    except OSError:
        pass


def _read_cache_meta(path: Path) -> dict:
    try:
        return json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 标准化
def market_prefix(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("0", "2", "3")):
        return "sz"
    return "bj"


def _normalize_frame(raw: pd.DataFrame, column_map: dict[str, str], vendor: str) -> tuple[pd.DataFrame, int]:
    """统一为英文列；无效行直接剔除并记录，绝不用插值编造数值。"""
    usable = {k: v for k, v in column_map.items() if k in raw.columns}
    if "date" not in usable.values() or "close" not in usable.values():
        raise ValueError(f"{vendor}: 返回结构缺少必要字段 date/close，实际列={list(raw.columns)}")
    out = raw[list(usable)].rename(columns=usable).copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in out.columns:
            out[col] = float("nan")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date"])
    out = out.sort_values("date").drop_duplicates("date", keep="last")
    before = len(out)
    valid = (out["close"] > 0) & (out["open"] > 0) & (out["high"] > 0) & (out["low"] > 0)
    out = out[valid]
    return out[PRICE_COLUMNS].reset_index(drop=True), before - len(out)


def _clip_to_as_of(df: pd.DataFrame, start: date, end: date, as_of: date) -> pd.DataFrame:
    """P0-2：无论上游返回多少，末端一律钳到 as_of 之前，杜绝未来函数。"""
    upper = min(end, as_of).isoformat()
    lower = start.isoformat()
    return df[(df["date"] >= lower) & (df["date"] <= upper)].reset_index(drop=True)


# ---------------------------------------------------------------- vendor 实现
_EM_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}
_EN_MAP = {
    "date": "date", "open": "open", "close": "close", "high": "high",
    "low": "low", "volume": "volume", "amount": "amount",
}


def _fetch_stock_em(code: str, start: date, end: date, adjust: str) -> pd.DataFrame:
    fn = getattr(ak, "stock_zh_a_hist_em", None) or getattr(ak, "stock_zh_a_hist")
    return fn(symbol=code, period="daily",
              start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
              adjust=adjust)


def _fetch_stock_sina(code: str, start: date, end: date, adjust: str) -> pd.DataFrame:
    fn = getattr(ak, "stock_zh_a_hist_sina", None) or getattr(ak, "stock_zh_a_daily")
    return fn(symbol=f"{market_prefix(code)}{code}",
              start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
              adjust=adjust)


def _fetch_bench_sina(meta: dict, start: date, end: date) -> pd.DataFrame:
    fn = getattr(ak, "index_zh_daily_sina", None) or getattr(ak, "stock_zh_index_daily")
    return fn(symbol=meta["sina"])  # 返回全历史，由 _clip_to_as_of 本地裁剪


def _fetch_bench_tx(meta: dict, start: date, end: date) -> pd.DataFrame:
    fn = getattr(ak, "index_zh_daily_tx", None) or getattr(ak, "stock_zh_index_daily_tx")
    return fn(symbol=meta["tx"])


def _fetch_bench_em(meta: dict, start: date, end: date) -> pd.DataFrame:
    fn = getattr(ak, "index_zh_hist_em", None) or getattr(ak, "index_zh_a_hist")
    return fn(symbol=meta["em"], period="daily",
              start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))


# 声明式有序链：显式列出，绝不偷偷追加未配置的数据源
STOCK_VENDORS: list[dict[str, Any]] = [
    {"name": "akshare:stock_zh_a_hist_em(东方财富/前复权)", "vendor": "eastmoney",
     "throttle": "eastmoney", "map": _EM_MAP,
     "fetch": lambda c, s, e, a: _fetch_stock_em(c, s, e, a)},
    {"name": "akshare:stock_zh_a_hist_sina(新浪/前复权)", "vendor": "sina",
     "throttle": "sina", "map": _EN_MAP,
     "fetch": lambda c, s, e, a: _fetch_stock_sina(c, s, e, a)},
]

BENCH_VENDORS: list[dict[str, Any]] = [
    {"name": "akshare:index_zh_daily_sina(新浪)", "vendor": "sina",
     "throttle": "sina", "map": _EN_MAP,
     "fetch": lambda m, s, e: _fetch_bench_sina(m, s, e)},
    {"name": "akshare:index_zh_daily_tx(腾讯)", "vendor": "tx",
     "throttle": "tx", "map": _EN_MAP,
     "fetch": lambda m, s, e: _fetch_bench_tx(m, s, e)},
    {"name": "akshare:index_zh_hist_em(东方财富)", "vendor": "eastmoney",
     "throttle": "eastmoney", "map": _EM_MAP,
     "fetch": lambda m, s, e: _fetch_bench_em(m, s, e)},
]


# ---------------------------------------------------------------- 结果对象
@dataclass
class DataResult:
    status: str
    frame: pd.DataFrame | None
    vendor: str = ""
    coverage: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == Sentinel.OK and self.frame is not None and not self.frame.empty

    def as_quality(self) -> dict:
        return {
            "status": self.status,
            "vendor": self.vendor or "N/A",
            "coverage": self.coverage,
            "notes": list(self.notes),
        }


def _failed(kind: str, notes: list[str]) -> DataResult:
    return DataResult(status=kind, frame=None, notes=notes)


def fetch_price_series(
    kind: str,
    code: str,
    start: date,
    end: date,
    as_of: date,
    adjust: str = "qfq",
) -> DataResult:
    """按显式有序链取数；链穷尽后返回带指令的哨兵，绝不返回空表冒充成功。"""
    if kind not in ("stock", "benchmark"):
        return _failed(Sentinel.DATA_UNAVAILABLE, ["未知数据种类"])

    bench_meta = get_benchmark(code) if kind == "benchmark" else None
    cache_key = f"{kind}_{code}_{adjust}_{start.isoformat()}_{end.isoformat()}_{as_of.isoformat()}"
    cache_path = _cache_file(cache_key)
    cached = _read_cache(cache_path, PRICE_CACHE_TTL)
    if cached is not None and not cached.empty:
        clipped = _clip_to_as_of(cached, start, end, as_of)
        if len(clipped) >= 2:
            meta = _read_cache_meta(cache_path)
            origin = meta.get("vendor", "disk-cache")
            coverage = _coverage(clipped, start, end)
            coverage["dropped_invalid_rows"] = int(meta.get("dropped_invalid_rows", 0))
            cache_notes = [f"命中本地磁盘缓存，原始数据来自 {origin}，本次未产生外部请求"]
            cache_notes.extend(meta.get("notes", []))
            return DataResult(
                status=Sentinel.OK, frame=clipped, vendor=origin,
                coverage=coverage, notes=cache_notes,
            )

    chain = STOCK_VENDORS if kind == "stock" else BENCH_VENDORS
    notes: list[str] = []
    for spec in chain:
        name = spec["name"]
        try:
            _THROTTLES[spec["throttle"]].wait()
            if kind == "stock":
                raw = spec["fetch"](code, start, end, adjust)
            else:
                raw = spec["fetch"](bench_meta, start, end)
            if raw is None or len(raw) == 0:
                notes.append(f"[{name}] 返回空数据 -> NoMarketDataError（与标的有关，停止回落）")
                return _failed(Sentinel.NO_DATA_AVAILABLE, notes)
            normalized, dropped = _normalize_frame(raw, spec["map"], name)
            clipped = _clip_to_as_of(normalized, start, end, as_of)
            if len(clipped) < 2:
                notes.append(f"[{name}] as_of 钳制后不足 2 个交易日 -> NoMarketDataError")
                return _failed(Sentinel.NO_DATA_AVAILABLE, notes)
            coverage = _coverage(clipped, start, end)
            coverage["dropped_invalid_rows"] = dropped
            if dropped:
                notes.append(f"[{name}] 剔除 {dropped} 行无效/缺失行情，未做任何插值补齐")
            if spec["vendor"] == "eastmoney":
                notes.append(f"[{name}] 东财 push2 已节流调用")
            _atomic_write_csv(normalized, cache_path)
            _write_cache_meta(cache_path, {
                "vendor": name, "dropped_invalid_rows": dropped, "notes": notes,
            })
            return DataResult(status=Sentinel.OK, frame=clipped, vendor=name,
                              coverage=coverage, notes=notes)
        except Exception as exc:  # 工具内异常不抛出，避免中断整条流水线
            kind_err = classify_exception(exc)
            short = str(exc).replace("\n", " ")[:160]
            notes.append(f"[{name}] {kind_err.value}: {short}")
            if kind_err == VendorErrorKind.NO_MARKET_DATA:
                break
            continue

    notes.append(INSTRUCTIONS[Sentinel.DATA_UNAVAILABLE])
    return _failed(Sentinel.DATA_UNAVAILABLE, notes)


def _coverage(df: pd.DataFrame, start: date, end: date) -> dict:
    """数据覆盖度是可分享报告的一等输出（对应文档 5-4 改进建议）。"""
    return {
        "rows": int(len(df)),
        "first_date": str(df["date"].iloc[0]),
        "last_date": str(df["date"].iloc[-1]),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "dropped_invalid_rows": 0,
    }


# ---------------------------------------------------------------- 股票名录
FALLBACK_NAMES: list[tuple[str, str]] = [
    ("600519", "贵州茅台"), ("601318", "中国平安"), ("600036", "招商银行"),
    ("000001", "平安银行"), ("000002", "万科A"), ("000858", "五粮液"),
    ("300750", "宁德时代"), ("002594", "比亚迪"), ("600900", "长江电力"),
    ("601899", "紫金矿业"), ("600276", "恒瑞医药"), ("002415", "海康威视"),
    ("300059", "东方财富"), ("600030", "中信证券"), ("601012", "隆基绿能"),
    ("000333", "美的集团"), ("000651", "格力电器"), ("600887", "伊利股份"),
    ("601888", "中国中免"), ("603259", "药明康德"), ("688981", "中芯国际"),
    ("688111", "金山办公"), ("600309", "万华化学"), ("002475", "立讯精密"),
    ("601166", "兴业银行"), ("600000", "浦发银行"), ("601398", "工商银行"),
    ("601857", "中国石油"), ("600028", "中国石化"), ("601988", "中国银行"),
    ("600703", "三安光电"), ("002230", "科大讯飞"), ("300015", "爱尔眼科"),
    ("600585", "海螺水泥"), ("601288", "农业银行"), ("600690", "海尔智家"),
    ("000568", "泸州老窖"), ("002304", "洋河股份"), ("600809", "山西汾酒"),
    ("603288", "海天味业"), ("601601", "中国太保"), ("300124", "汇川技术"),
]


def _clean_name(value: str) -> str:
    return "".join(str(value).split()).replace("\u3000", "")


def _universe_from_exchanges() -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    notes: list[str] = []
    sources = [
        ("上交所名录 stock_info_sh_name_code", "exchange",
         getattr(ak, "stock_info_sh_name_code", None), "证券代码", "证券简称", "SH"),
        ("深交所名录 stock_info_sz_name_code", "exchange",
         getattr(ak, "stock_info_sz_name_code", None), "A股代码", "A股简称", "SZ"),
    ]
    for label, throttle_key, fn, code_col, name_col, market in sources:
        try:
            if fn is None:
                raise AttributeError(f"{label} 在当前 akshare 版本中不存在")
            _THROTTLES[throttle_key].wait()
            df = fn()
            for _, row in df.iterrows():
                code = str(row[code_col]).zfill(6)
                if not code.isdigit() or len(code) != 6:
                    continue
                items.append({"code": code, "name": _clean_name(row[name_col]), "market": market})
            notes.append(f"{label}：载入 {len(df)} 行")
        except Exception as exc:
            notes.append(f"{label} 不可用（{type(exc).__name__}）：{str(exc)[:120]}；已跳过，不编造名录")
    return items, notes


def load_universe() -> dict:
    """名录属于可选数据：单源失败只降级留痕，主流程不中断。"""
    path = CACHE_DIR / "universe.json"
    try:
        if path.exists() and time.time() - path.stat().st_mtime < UNIVERSE_CACHE_TTL:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("count", 0) > 100:
                return payload
    except Exception:
        pass

    items, notes = _universe_from_exchanges()
    dedup: dict[str, dict] = {}
    for item in items:
        dedup[item["code"]] = item
    merged = list(dedup.values())
    source = "akshare 沪深交易所官方名录"
    if len(merged) < 100:
        merged = [{"code": c, "name": n, "market": market_prefix(c).upper()}
                  for c, n in FALLBACK_NAMES]
        source = "内置兜底名录（交易所名录不可用）"
        notes.append(INSTRUCTIONS[Sentinel.DATA_UNAVAILABLE])

    payload = {
        "source": source,
        "count": len(merged),
        "items": sorted(merged, key=lambda x: x["code"]),
        "notes": notes,
    }
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return payload


def search_symbols(query: str, limit: int = 12) -> list[dict]:
    q = _clean_name(query).upper()
    if not q:
        return []
    universe = load_universe()
    pool = universe["items"]
    exact = [x for x in pool if x["code"] == q]
    prefix = [x for x in pool if x["code"].startswith(q) and x not in exact]
    named = [x for x in pool if q in _clean_name(x["name"]).upper() and x not in exact and x not in prefix]
    result = (exact + prefix + named)[:limit]
    if q.isdigit() and len(q) == 6 and not any(x["code"] == q for x in result):
        result.append({"code": q, "name": "名录未收录（将按代码直接查询）",
                       "market": market_prefix(q).upper(), "unlisted": True})
    return result
