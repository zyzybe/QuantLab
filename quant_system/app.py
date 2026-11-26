"""
QuantLab —— 动态量化回测 Web 应用（Flask 后端）
================================================================
启动：  python app.py
访问：  http://127.0.0.1:5000

四个模块：
  1 数据接入   quant/datafeed.py   akshare 有序数据源链 + as_of 钳制 + 原子缓存 + 哨兵
  2 策略信号   quant/signals.py    4 位并行分析员 -> 多空辩论 -> 裁判 -> 风控
  3 回测引擎   quant/backtest.py   t+1 开盘成交 + 手续费/整手/止损冷却
  4 结果输出   quant/metrics.py + quant/reporting.py  净值曲线 + 四大指标 + 基准横向对比
  4c 研究建议  quant/advisory.py                    基于回测数值的规则化策略评估与研究动作建议

架构要点来自《TradingAgents_可借鉴设计要点_技术文档》：
  交付物契约 / 不可信参数注入 / 指令式哨兵 / REVIEW 非 HOLD / 显式缺失标记 /
  结构化输出 / 元信息白名单 / 运行签名 / 防未来函数。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from quant.contracts import APP_VERSION, Sentinel
from quant.datafeed import BENCHMARKS, DEFAULT_BENCHMARK, load_universe, search_symbols
from quant.pipeline import build_context, run_pipeline
from quant.reporting import error_report

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
RESULT_CACHE_TTL = 600
_result_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")


def _json(payload: dict, status_code: int = 200):
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp, status_code


@app.after_request
def _no_cache(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.get("/")
def index():
    return send_from_directory(str(STATIC), "index.html")


@app.get("/api/health")
def health():
    return _json({
        "status": Sentinel.OK,
        "app": "QuantLab",
        "version": APP_VERSION,
        "akshare": "已接入",
        "universe_count": load_universe()["count"],
        "benchmarks": [{"code": b["code"], "name": b["name"]} for b in BENCHMARKS],
    })


@app.get("/api/config")
def config():
    today = date.today()
    return _json({
        "default_symbol": "600519",
        "default_start": (today - timedelta(days=365 * 3)).isoformat(),
        "default_end": today.isoformat(),
        "benchmark": DEFAULT_BENCHMARK,
        "benchmarks": [{"code": b["code"], "name": b["name"]} for b in BENCHMARKS],
        "parameters": {
            "initial_capital": 1000000, "max_position": 0.95, "stop_loss": 0.12,
            "cooldown_days": 5, "fast_window": 20, "slow_window": 60,
            "rsi_window": 14, "momentum_window": 20, "bollinger_window": 20,
            "volume_window": 20, "vote_threshold": 0.25, "rf": 0.02,
        },
    })


@app.get("/api/universe")
def universe():
    q = request.args.get("q", "").strip()
    try:
        return _json({"status": Sentinel.OK, "results": search_symbols(q),
                      "source": load_universe()["source"]})
    except Exception as exc:
        return _json(error_report(Sentinel.DATA_UNAVAILABLE,
                                  f"名录查询失败：{type(exc).__name__}", [str(exc)[:200]]), 200)


@app.route("/api/backtest", methods=["POST", "GET"])
def backtest():
    payload = dict(request.get_json(silent=True) or {}) if request.method == "POST" else dict(request.args)
    ctx, err = build_context(payload)
    if err:
        return _json(error_report("BAD_INPUT", err), 400)

    signature = ctx.signature()
    with _cache_lock:
        hit = _result_cache.get(signature)
        if hit and time.time() - hit[0] < RESULT_CACHE_TTL:
            cached = dict(hit[1])
            cached["from_cache"] = True
            return _json(cached)

    try:
        report = run_pipeline(ctx)
    except Exception as exc:
        # 任一未分类异常都转成哨兵响应，不把堆栈/本地路径泄露给调用方（3.7）
        traceback.print_exc()
        report = error_report(
            Sentinel.DATA_UNAVAILABLE,
            "回测流水线内部异常，未产出任何净值或指标",
            [f"{type(exc).__name__}: {str(exc)[:200]}"],
        )
        report["meta"] = {"run_signature": signature, "as_of": ctx.as_of.isoformat(),
                          "app_version": APP_VERSION}

    if report.get("status") == Sentinel.OK:
        with _cache_lock:
            _result_cache[signature] = (time.time(), report)
    return _json(report, 200 if report.get("status") == Sentinel.OK else 200)


def _warmup_universe() -> None:
    try:
        load_universe()
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=_warmup_universe, daemon=True).start()
    print(f"QuantLab {APP_VERSION} 启动： http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)
