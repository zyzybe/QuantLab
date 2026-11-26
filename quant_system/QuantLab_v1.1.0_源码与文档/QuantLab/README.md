# QuantLab · 动态量化回测 Web 应用

一个带 Python 后端的动态 Web 应用。在搜索栏输入股票代码或名称，系统自动接入 akshare 历史行情，依次经过 **数据接入 → 策略信号 → 回测引擎 → 结果输出** 四个模块，返回净值曲线、四项必需指标（累计收益率、年化收益率、最大回撤、夏普比率）、与基准的横向对比、交易明细，以及一份基于回测数值生成的规则化「投资研究建议」。

- 版本：`1.1.0`
- 技术栈：Python 3.12 + Flask + akshare + pandas / numpy + 原生 JavaScript（自绘 SVG，无外部 CDN）
- 默认基准：沪深300（`000300`），另可选上证指数、中证500、中证1000、创业板指
- 项目路径：`C:\Users\G2738\Desktop\11.25\quant_system`

---

## 1. 快速开始

### 1.1 先确认解释器（本机必看）

> ⚠️ 本机直接敲 `python` 指向的是 WorkBuddy 内置的 Python 3.13，**没有安装任何第三方库**，用它启动会报 `ModuleNotFoundError`。必须使用系统 Python 3.12：

```
C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe
```

验证依赖是否就绪：

```bat
"C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe" -c "import akshare, flask, pandas, numpy, requests; print('DEPS OK')"
```

`.vscode/settings.json` 已把 VS Code 的解释器锁定到该路径。

### 1.2 安装 / 更新依赖

```bat
cd /d C:\Users\G2738\Desktop\11.25\quant_system
"C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
```

依赖清单见 [`requirements.txt`](./requirements.txt)，含版本下限说明与实测版本对照。**akshare 必须 ≥ 1.20.65**：1.20.x 起旧接口全部改名，本项目使用的是 `stock_zh_a_hist_em` / `index_zh_daily_sina` / `stock_info_sh_name_code` 等新接口名。

### 1.3 启动服务

| 方式 | 操作 |
|---|---|
| A（最省事） | 双击项目根目录的 `run.bat` |
| B（命令行） | 在项目目录执行 `"C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe" app.py` |
| C（VS Code） | 打开项目文件夹，在终端执行方式 B 的命令 |

控制台出现 `QuantLab 1.1.0 启动： http://127.0.0.1:5000` 即启动成功。浏览器打开：

- 首页：<http://127.0.0.1:5000>
- 直接带默认结果的深链接：<http://127.0.0.1:5000/?autorun=1&symbol=600519>

服务只监听 `127.0.0.1:5000`（本机），首次启动会在后台预热约 4605 只沪深股票名录，属正常现象。停止服务：在运行窗口按 `Ctrl+C`，或关闭 `run.bat` 窗口。

---

## 2. 目录结构

```
quant_system/
├── app.py                     Flask 入口：REST API + 结果缓存 + 异常哨兵
├── quant/                     后端核心包
│   ├── __init__.py
│   ├── contracts.py           状态契约、哨兵枚举、RunContext / RunState、节点读写表
│   ├── datafeed.py            模块1 数据接入：akshare 有序源链、as_of 钳制、缓存、名录
│   ├── signals.py             模块2 策略信号：4 位并行分析员 → 辩论 → 裁判 → 风控
│   ├── backtest.py            模块3 回测引擎：T+1 开盘成交、手续费、整手、止损冷却
│   ├── metrics.py             模块4a 绩效指标：四项必需指标 + 基准横向对比
│   ├── reporting.py           模块4b 报告装配：元信息白名单、降级说明、免责声明
│   ├── advisory.py            模块4c 投资研究建议：五维评分、结论分档、研究动作
│   └── pipeline.py            编排层：参数校验 → 四模块流水线 → 报告
├── static/
│   ├── index.html             页面骨架（搜索栏、参数表单、结果区）
│   ├── style.css              浅色主题样式与响应式布局
│   └── app.js                 联想搜索、动态请求、SVG 净值曲线、各结果区渲染
├── .vscode/settings.json      锁定 Python 3.12 解释器
├── requirements.txt           运行依赖清单
├── run.bat                    一键启动脚本
├── 完整源代码与说明.md        全部源码 + 逐文件解释（本包附带）
├── cache/                     行情磁盘缓存（自动生成，可删除，删后重新联网取数）
├── report_assets/             技术报告用的真实回测数据与图表（可选）
└── preview*.png               结果页校验截图
```

---

## 3. 页面使用流程

1. **搜索标的**：在搜索栏输入 6 位代码（如 `600519`）或中文名称（如 `茅台`），下拉联想来自沪深交易所官方名录；也可点快捷选股。
2. **调整参数**（可选）：起止日期、基准指数、初始资金、快慢均线、最大仓位、止损线、冷却天数、投票阈值、无风险利率。页面会实时把参数提交给后端。
3. **点击「运行回测」**：页面四个模块卡片依次点亮，对应后端流水线阶段。
4. **查看结果**：四项指标卡 → 投资研究建议卡 → 三条净值曲线（策略 / 基准 / 买入持有）→ 横向对比表 → 交易明细 → 运行元信息 → 降级与执行说明。
5. **深链接**：`?autorun=1&symbol=600519&start=2023-11-26&end=2026-11-25` 可直接分享带结果的页面。

换股票或改参数后，建议模块会随之重新计算，不需要刷新服务。

---

## 4. REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页 |
| GET | `/api/health` | 服务状态、版本、名录数量、可用基准清单 |
| GET | `/api/config` | 默认标的、默认区间、全部默认参数 |
| GET | `/api/universe?q=茅台` | 代码/名称联想，返回匹配列表 |
| POST | `/api/backtest` | 执行回测（JSON 请求体） |
| GET | `/api/backtest?symbol=600519&...` | 同上，参数走 query string |
| GET | `/static/*` | 静态资源 |

最小调用示例：

```python
import requests
payload = {
    "symbol": "600519",
    "start_date": "2023-11-26",
    "end_date": "2026-11-25",
    "benchmark": "000300",
}
r = requests.post("http://127.0.0.1:5000/api/backtest", json=payload, timeout=180).json()
print(r["status"], r["metrics"]["strategy"]["cumulative_return"])
```

响应结构（成功时 `status = OK`）包含 `curves`（日期与三条净值）、`metrics`（策略 / 基准 / 买入持有 / 横向对比）、`trades`、`advisory`、`signal_summary`、`execution`、`meta`、`node_contract`、`degradation_notes`、`disclaimer`。失败时返回带指令的哨兵响应，而不是一串 0。

---

## 5. 回测参数与规则

| 参数 | 默认值 | 允许范围 | 含义 |
|---|---|---|---|
| `initial_capital` | 1,000,000 | 1万 ~ 1亿 | 初始资金（元） |
| `max_position` | 0.95 | 0.1 ~ 1.0 | 单次最大目标仓位 |
| `stop_loss` | 0.12 | 0.02 ~ 0.5 | 相对持仓均价的止损线 |
| `cooldown_days` | 5 | 0 ~ 30 | 止损后不开仓的交易日数 |
| `fast_window` / `slow_window` | 20 / 60 | 3~120 / 10~250 | 双均线窗口，快线必须小于慢线 |
| `rsi_window` / `momentum_window` | 14 / 20 | 3~60 / 3~120 | Wilder RSI 与 ROC 窗口 |
| `bollinger_window` / `volume_window` | 20 / 20 | 5~120 | 布林带与量比长周期窗口 |
| `vote_threshold` | 0.25 | 0 ~ 1 | 综合得分超过 ±阈值才产生 BUY / SELL |
| `rf` | 0.02 | 0 ~ 0.1 | 年化无风险利率（夏普比率用） |
| `commission_rate` / `stamp_tax` | 0.00025 / 0.0005 | 0 ~ 0.005 | 佣金（最低 5 元）/ 卖出印花税 |

**执行模型**：t 日收盘后生成信号，t+1 **开盘价**成交；每日最多调仓一次；100 股整手优先，一手都买不起时启用零股演示并在报告中显式标注；止损收盘判定、次日开盘强平；个股与基准交易日只取交集，不插值、不前向填充；服务端强制 `as_of = min(end_date, today)`，前端无法取得未来数据。

**四项指标**：累计收益率 = 期末净值/期初净值 − 1；年化收益率按 252 交易日折算；最大回撤为净值相对历史峰值的最大跌幅；夏普比率 = (日均收益 − rf/252) / 日收益标准差 × √252。样本不足或波动为 0 时返回 `null`（页面显示「—」），不填 0。

---

## 6. 数据来源与已知坑

| 用途 | akshare 接口（有序链） |
|---|---|
| 个股日线（前复权） | `stock_zh_a_hist_em` → `stock_zh_a_hist_sina` |
| 基准指数日线 | `index_zh_daily_sina` → `index_zh_daily_tx` → `index_zh_hist_em` |
| 股票代码名称表 | `stock_info_sh_name_code` + `stock_info_sz_name_code` |

- 东方财富 push2 接口在并发时会触发限流，因此做了**节流 + 有序链回落 + 磁盘缓存**（行情缓存 6 小时、名录 7 天，写在 `cache/`）。命中缓存时报告元信息仍显示真实数据源。
- 新浪指数返回全历史，由 `_clip_to_as_of` 在本地裁剪；沪深交易所名录不依赖东财，属可选数据，单源失败只降级留痕、不中断主流程。
- 每个数据源失败都会返回带指令的哨兵（`DATA_UNAVAILABLE` / `COVERAGE_GAP` 等），绝不返回空表冒充成功；`REVIEW` 表示"本轮未判定"，**不等同于 HOLD**，不产生任何交易动作。
- 所有报告都带 `run_signature`（覆盖全部影响产出的参数），相同参数结果可复现；元信息经白名单过滤，不泄露本地路径或密钥。

---

## 7. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `ModuleNotFoundError: No module named 'akshare'` | 解释器用错。改用第 1.1 节的 Python 3.12 绝对路径，或双击 `run.bat` |
| 首次回测很慢 / 超时 | 需要联网抓历史行情；成功后写入 `cache/`，后续命中缓存会快很多 |
| 返回 `DATA_UNAVAILABLE` 或降级说明 | 上游限流或该区间无数据，稍后重试；报告里会列出实际命中的源与覆盖度 |
| 端口 5000 被占用 | `app.py` 末行改 `port=5001`；或用 PowerShell 结束占用进程：`Get-NetTCPConnection -LocalPort 5000 -State Listen | Select -First 1 -Expand OwningProcess` 后 `Stop-Process -Id <PID>` |
| 0 笔交易 | 高价股一手金额超过可用资金，或区间过短。默认资金已设为 100 万，并有零股兜底；可提高资金或延长区间 |
| 指标显示「—」 | 样本不足 / 波动为 0，按哨兵返回 `null`，这是设计行为不是 bug |
| 建议卡显示「证据不足」 | 有效交易日 < 60 或成交 < 2 笔时不形成综合评分，页面会列出未达门槛的具体原因 |
| 中文乱码 | 终端切换到 UTF-8（`chcp 65001`）；源码与缓存均为 UTF-8 |

---

## 8. 复现与验证

```bat
cd /d C:\Users\G2738\Desktop\11.25\quant_system
"C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe" -m compileall -q quant app.py
"C:\Users\G2738\AppData\Local\Programs\Python\Python312\python.exe" -c "import requests; print(requests.get('http://127.0.0.1:5000/api/health').json())"
```

`report_assets/` 中保存了三段真实回测结果（全区间 / 样本内 / 样本外）与三张由这些数据绘制的图表，是项目技术报告的数据底稿；`preview.png` 与 `preview-insufficient.png` 是结果页与「证据不足」档的渲染校验截图。

逐文件的职责、关键函数、设计要点与**全部 3317 行源代码**，见同目录的 [`完整源代码与说明.md`](./完整源代码与说明.md)。

---

## 9. 边界声明

本系统是量化研究与工程教学演示工具，采用简化执行模型（次日开盘成交、固定费率、不含滑点、涨跌停与停牌约束）。所有指标与建议均来自历史数据在既定规则下的计算结果，不对任何标的作评价、不预测行情、不承诺收益；「投资研究建议」模块只评估策略参数组合与历史区间，不含买卖意见。历史回测不代表未来表现，不构成投资建议。
