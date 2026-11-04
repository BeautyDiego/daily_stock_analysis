<div align="center">

# 📈 股票智能分析系统

[![GitHub stars](https://img.shields.io/github/stars/ZhuLinsen/daily_stock_analysis?style=social)](https://github.com/ZhuLinsen/daily_stock_analysis/stargazers)
[![arXiv](https://img.shields.io/badge/arXiv-2608.26990-b31b1b.svg)](https://arxiv.org/abs/2608.26990)
[![CI](https://github.com/ZhuLinsen/daily_stock_analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/ZhuLinsen/daily_stock_analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/zhulinsen/daily_stock_analysis)

<p align="center">
  <img src="https://trendshift.io/api/badge/trendshift/repositories/18527/daily?language=Python" alt="#1 Python Repository Of The Day | Trendshift" width="250" height="55"/>&nbsp;<a href="https://hellogithub.com/repository/ZhuLinsen/daily_stock_analysis" target="_blank"><img src="https://api.hellogithub.com/v1/widgets/recommend.svg?rid=6daa16e405ce46ed97b4a57706aeb29f&claim_uid=pfiJMqhR9uvDGlT&theme=neutral" alt="Featured｜HelloGitHub" width="230" /></a>
</p>

> 🤖 基于 AI 大模型的 A股/港股/美股/日股/韩股/台股自选股智能分析系统，每日自动分析并推送「决策仪表盘」到企业微信/飞书/Telegram/Discord/Slack/邮箱
>
> 📊 **当前部署形态**：已收敛为 **A 股自动选股 + 模拟盘自动交易** 闭环——每日自动选股、按评分卡复盘、程序化下单与止损止盈，只在开盘和收盘各推一条简报。详见 [A 股自动交易系统](#-a-股自动交易系统)。

[**产品预览**](#-产品预览) · [**功能特性**](#-功能特性) · [**A 股自动交易**](#-a-股自动交易系统) · [**快速开始**](#-快速开始) · [**推送效果**](#-推送效果) · [**文档中心**](docs/INDEX.md) · [**完整指南**](docs/full-guide.md)

简体中文 | [English](docs/README_EN.md) | [繁體中文](docs/README_CHT.md)

</div>

## 💖 赞助商 (Sponsors)
<div align="center">
  <p align="center">
    <a href="https://open.anspire.cn/dsa?share_code=QFBC0FYC" target="_blank"><img src="./docs/assets/anspire.png" alt="Anspire Open 一站式模型和搜索服务" width="300" height="141" style="width: 300px; height: 141px; object-fit: contain;"></a>
    <a href="https://serpapi.com/github-daily-stock-analysis" target="_blank"><img src="./docs/assets/serpapi_banner_zh.png" alt="轻松抓取搜索引擎上的实时金融新闻数据 - SerpApi" width="300" height="141" style="width: 300px; height: 141px; object-fit: contain;"></a>
  </p>
</div>


## 🖥️ 产品预览

<p align="center">
  <img src="docs/assets/readme_workspace_tour_20260510.gif" alt="DSA Web 工作台演示" width="720">
</p>

## ✨ 功能特性

| 能力 | 覆盖内容 |
|------|------|
| AI 决策报告 | 核心结论、评分、趋势、买卖点位、风险警报、催化因素、操作检查清单 |
| 多市场数据聚合 | 覆盖 A股、港股、美股、日股、韩股、台股和 ETF，支持行情、K 线、技术指标、新闻、公告、基本面与报告辅助数据；不同市场的数据源和能力边界见 [市场支持边界](docs/market-support.md) |
| Web / 桌面工作台 | 手动分析、任务进度、历史报告、完整 Markdown、回测、持仓、配置管理、浅色 / 深色主题 |
| Agent 策略问股 | 多轮追问，支持均线、缠论、波浪、趋势、热点、事件、成长、预期等 15 种内置策略，覆盖 Web/Bot/API |
| 智能导入与补全 | 图片、CSV/Excel、剪贴板导入；股票代码/名称/拼音/别名补全 |
| 自动化与推送 | GitHub Actions、Docker、本地定时任务、FastAPI 服务和企业微信/飞书/Telegram/Discord/Slack/邮件推送 |
| **A 股自动交易** | 自动选股 → 评分卡复盘 → 程序化下单 → 盘中止损止盈 → 收盘结算的完整闭环，含组合级风控、T+1 约束、模拟盘账本与 `/paper` 看板 |

> 功能细节、字段契约、基本面 P0 超时语义、交易纪律、数据源优先级、Web/API 行为请看 [完整配置与部署指南](docs/full-guide.md)。

### 技术栈与数据来源

| 类型 | 支持 |
|------|------|
| AI 模型 | [Anspire](https://open.anspire.cn/dsa?share_code=QFBC0FYC)、[AIHubMix](https://inferera.com/?aff=CfMq)、Gemini、OpenAI 兼容、DeepSeek、通义千问、Claude、Ollama 本地模型等 |
| 行情数据 | [TickFlow](https://tickflow.org/auth/register?ref=WDSGSPS5XC)、AkShare、Tushare、Pytdx、Baostock、YFinance、Longbridge |
| 新闻搜索 | [Anspire](https://open.anspire.cn/dsa?share_code=QFBC0FYC)、[SerpAPI](https://serpapi.com/github-daily-stock-analysis)、[Tavily](https://tavily.com/)、[Bocha](https://open.bocha.cn/)、[Brave](https://brave.com/search/api/)、[MiniMax](https://platform.minimaxi.com/)、SearXNG |
| 社交舆情 | [Stock Sentiment API](https://api.adanos.org/docs)（Reddit / X / Polymarket，仅美股，可选） |

> 项目默认内置 AkShare、Baostock、YFinance 等免费行情源，可零配置运行；免费源受上游限流、接口变动和网络波动影响，稳定性不保证。长期定时、批量分析或更稳定行情建议配置 TickFlow、Tushare、Longbridge 等 token 型数据源，适用市场、Actions 映射和 fallback 规则见 [数据源配置](docs/full-guide.md#数据源配置)。

## 📊 A 股自动交易系统

> 本节描述当前仓库的**实际运行形态**。上游的多市场 AI 分析、Agent 问股、回测等能力仍保留在代码中，
> 只是当前部署只启用 A 股自动交易链路。

### 一天的时间线（工作日）

| 时间 | 动作 | 说明 |
|------|------|------|
| 08:45 | 采集宏观政策 | 政策新闻结构化为「行业 + 方向 + 强度」事件，落库可回溯 |
| 09:10 | 持仓复盘 | 5 维评分卡（趋势 30 / 相对强度 20 / 量能 15 / 风险 20 / 政策 15）→ `hold` / `reduce` / `exit` |
| 09:25 | 生成订单 | 买 = 上一个选股日的结果；卖 = 当日复盘建议。先卖后买，卖出资金当日可用于建仓 |
| 09:45 | 开盘成交 | 按实时价成交；高开超 3% 放弃、跌停跳过、现金不足自动降手数 |
| 10 / 11 / 13 / 14 点 | 盘中风控 | 实时价检查止损止盈，触发即清仓（遵守 T+1，当日买入不可卖） |
| 15:10 | 同步日线 | 持仓 + 近期选股 + 双基准（510300 判市场环境、512100 算复盘超额） |
| 15:15 | 收盘结算 | 估值、当日与累计盈亏、收盘简报 |
| 16:20 | 每日选股 | 全市场筛选 → 因子打分 → 政策加成 → 去重与行业分散 → Top 5 |
| 16:40 | 次日验证 | 记录 T+1 涨跌与超额收益，供胜率统计 |

### 选股逻辑

四道筛子逐层收窄：

1. **硬过滤**：价格 3–20 元、市值 ≥ 50 亿、日成交额 ≥ 1 亿、涨跌幅 -4% ~ +8.5%、
   `0 < PE ≤ 80`、`0 < PB ≤ 8`，剔除 ST 与退市风险标的
2. **因子打分**（权重经全市场分层回测校准，5532 只 × 120 个交易日）：

   | 因子 | 权重 | 回测结论 |
   |------|------|----------|
   | value（估值） | 0.30 | 方向未被否定，承接释放的权重 |
   | stability（低波动） | 0.28 | **正向有效**：IC +0.11，IC>0 比例 73% |
   | size（规模） | 0.12 | 未验证 |
   | theme_heat（题材） | 0.10 | 未验证 |
   | momentum（动量） | 0.05 | **反向**：IC -0.09，已从 0.20 降权 |
   | liquidity（成交额） | 0.05 | **反向**：IC -0.11，已从 0.18 降权 |
   | activity（量比） | 0.05 | **反向**：IC -0.05，已从 0.15 降权 |
   | reversal（反转） | 0.05 | 无区分度 |

3. **政策加成**：命中利好行业 +2 ~ +5 分，命中利空 -3 ~ -8 分，单票上限 ±10 分
4. **去重与分散**：5 个交易日内不重复推荐、同行业最多 2 只、已持仓不重复买入；
   强政策命中且分数上涨超 5 分可破例，每日最多 2 只

### 风控参数

| 规则 | 参数 | 触发后 |
|------|------|--------|
| 硬止损 | **2×ATR20**（限 5%~15%） | 盘中清仓 |
| 硬止盈 | **4×ATR20**（限 10%~40%） | 盘中清仓 |
| 组合回撤 | **-10%**（近 60 个快照峰值起算） | 暂停开新仓，只允许卖出 |
| 单日亏损 | **-3%** | 当日不再开新仓 |
| 熊市降仓 | 510300 收盘跌破 MA20 | 仓位上限 5 → 2（单仓预算不变，多出来的自动留现金） |
| 滑点 | <5 元 0.4% / 5–15 元 0.25% / >15 元 0.15% | 计入成交价 |
| T+1 | 当日买入当日不可卖 | 含盘中风控 |

**已用数据否决的规则**（`--take-profit-study`，88,317 个买入点）：

- **分批止盈**：平均收益仅高 0.018pct，但多一笔卖出要多付约 0.13% 佣金，净效果为负
- **移动止盈**（+10% 激活、回落 5%）：比一次性硬止盈平均差 0.26pct，
  因为它只在「先涨到 +10%」的标的上启动——卖飞赢家、又不保护输家

### 模拟盘机制

- 初始资金 2 万元；仓位数量按账户权益自动计算（目标单仓 4000 元，限 1–10 仓）
- 成本全计：佣金万 2.5（最低 5 元）、印花税千 1、按价格分档的滑点
- 账本幂等：`dedup_hash` 保证重复执行不重复记账，可安全补跑历史交易日
- 复盘与日报口径统一：已持仓标的按**含佣金成本**算浮盈，未持仓的关注标的按推荐价

### 看板

`/paper` 页面（侧边栏「模拟盘」）：总资产与累计收益、SVG 净值曲线、持仓浮盈表、
近期成交及原因（选股/复盘离场/止损/止盈）、风控参数一览。

### 常用命令

```bash
# 自动交易链路（cron 已配置，通常无需手动执行）
python main.py --paper-plan        # 09:25 生成订单
python main.py --paper-execute     # 09:45 按实时价成交
python main.py --paper-watch       # 盘中止损止盈检查
python main.py --paper-settle      # 15:15 收盘结算

# 选股与复盘
python main.py --screen-picks --picks-market cn
python main.py --review-holdings --no-notify
python main.py --sync-daily

# 手动干预
python main.py --paper-trade-now                              # 用最新信号立即建仓
python main.py --mark-position 600016:closed:3.90:0:2026-11-02 # 补记历史交割

# 参数验证（离线回测，不影响线上）
python main.py --factor-layering --layering-pool 0   # 因子分层回测（0 = 全市场）
python main.py --take-profit-study                   # 止盈方式对比
python main.py --portfolio-study                     # 行业上限 / 冷却期对比
```

### 相关文档

| 文档 | 内容 |
|------|------|
| [持仓跟踪与模拟盘设计](docs/cn-holding-tracker-design.md) | 数据模型、评分卡、状态机、配置项、部署注意 |
| [交易系统改进路线](docs/cn-trading-improvement-plan.md) | 已识别缺陷、P0/P1/P2 任务、实盘放行标准与时间表 |

## 🚀 快速开始

### 方式一：[GitHub Actions（推荐）](https://www.bilibili.com/video/BV11FEb66EXG/)

> 5 分钟完成部署，零成本，无需服务器。


#### 1. Fork 本仓库

点击右上角 `Fork` 按钮（顺便点个 Star⭐ 支持一下）

#### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

**AI 模型配置（至少配置一个）**

默认先选一个模型服务商并填写 API Key；需要多模型、图片识别、本地模型或高级路由时，再参考 [LLM 配置指南](docs/LLM_CONFIG_GUIDE.md)。

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `ANSPIRE_API_KEYS` | [Anspire](https://open.anspire.cn/dsa?share_code=QFBC0FYC) API Key，一Key同时启用全球热门大模型和联网搜索，本项目新用户提供30元等额的免费额度（GLM5.2、GPT等模型特惠中） | **推荐** |
| `AIHUBMIX_KEY` | [AIHubMix](https://inferera.com/?aff=CfMq) API Key，一Key切换使用全系模型，无需科学上网，本项目可享 10% 优惠 | **推荐** |
| `GEMINI_API_KEY` | Google Gemini API Key | 可选 |
| `ANTHROPIC_API_KEY` | Anthropic Claude API Key | 可选 |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key（支持 DeepSeek、通义千问等） | 可选 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | 使用 OpenAI 兼容服务时填写 | 可选 |

> Ollama 更适合本地 / Docker 部署，GitHub Actions 推荐使用云端 API。

**通知渠道配置（至少配置一个）**

| Secret 名称 | 说明 |
|------------|------|
| `WECHAT_WEBHOOK_URL` | 企业微信机器人 |
| `FEISHU_WEBHOOK_URL` | 飞书机器人 |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |
| `DISCORD_WEBHOOK_URL` | Discord Webhook |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | Slack Bot |
| `EMAIL_SENDER` + `EMAIL_PASSWORD` | 邮件推送 |

更多渠道、签名校验、分组邮件、Markdown 转图片等配置见 [通知渠道详细配置](docs/full-guide.md#通知渠道详细配置)。

**自选股配置（必填）**

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `STOCK_LIST` | 自选股代码，如 `600519,hk00700,AAPL,7203.T,005930.KS,2330.TW`；已登记指数显式代码（如 `sh000016`、`930606.CSI`）经一次性 `--stocks` 或 GitHub Actions 入口支持，规则见 [指数自选股配置](docs/full-guide.md#指数自选股配置) | ✅ |

**新闻源配置（推荐）**

新闻源会显著影响舆情、公告、事件和催化因素质量，建议至少配置一个搜索服务。

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `ANSPIRE_API_KEYS` | [Anspire AI Search](https://open.anspire.cn/dsa?share_code=QFBC0FYC)：汇聚全球舆情信息，适配A股、美股、港股等新闻和舆情检索；同一Key可复用大模型服务，本项目新用户提供免费30元等额的免费点数 | **推荐** |
| `SERPAPI_API_KEYS` | [SerpAPI](https://serpapi.com/github-daily-stock-analysis)：搜索引擎结果补强，适合实时金融新闻 | **推荐** |
| `TAVILY_API_KEYS` | [Tavily](https://tavily.com/)：通用新闻搜索 API | 可选 |
| `BOCHA_API_KEYS` | [博查搜索](https://open.bocha.cn/)：中文搜索优化，支持 AI 摘要 | 可选 |
| `BRAVE_API_KEYS` | [Brave Search](https://brave.com/search/api/)：隐私优先，美股资讯补强 | 可选 |
| `MINIMAX_API_KEYS` | [MiniMax](https://platform.minimaxi.com/)：结构化搜索结果 | 可选 |
| `SEARXNG_BASE_URLS` | SearXNG 自建实例：无配额兜底，适合私有部署 | 可选 |

更多搜索源、社交舆情和降级规则见 [搜索服务配置](docs/full-guide.md#搜索服务配置)。

**行情数据源配置（可选）**

> 默认使用 AkShare、Baostock、YFinance 等免费数据源，日志中"未配置"的提示不影响运行。
> 如需更稳定的行情，可按市场配置以下 Secret：

| Secret 名称 | 适用市场 | 说明 |
|------------|:--------:|------|
| `TUSHARE_TOKEN` | A 股 | 提升历史行情稳定性 |
| `LONGBRIDGE_OAUTH_CLIENT_ID` + `LONGBRIDGE_OAUTH_TOKEN_CACHE_B64` | 港股/美股 | 补齐量比、换手率、PE 等字段 |

> 详见 [数据源配置](docs/full-guide.md#数据源配置)。

#### 3. 启用 Actions

`Actions` 标签 → `I understand my workflows, go ahead and enable them`

#### 4. 手动测试

`Actions` → `每日股票分析` → `Run workflow` → `Run workflow`

#### 完成

默认每个**工作日 18:00（北京时间）**自动执行，也可手动触发。默认非交易日（含 A/H/US 节假日）不执行；强制运行、交易日检查、断点续传等规则见 [完整指南](docs/full-guide.md#定时任务配置)。

### 方式二：[客户端配置教程](https://www.bilibili.com/video/BV11FEb66Eyr/) / 本地运行 / Docker 部署

```bash
# 克隆项目
git clone https://github.com/ZhuLinsen/daily_stock_analysis.git && cd daily_stock_analysis

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env && vim .env

# 运行分析
python main.py
```

常用命令：

```bash
python main.py --debug
python main.py --dry-run
python main.py --stocks 600519,hk00700,AAPL,2330.TW
python main.py --market-review
python main.py --schedule
python main.py --serve-only
```

> Docker 部署、定时任务、云服务器访问请参考 [完整指南](docs/full-guide.md)；桌面客户端打包请参考 [桌面端打包说明](docs/desktop-package.md)。

## 📱 推送效果

### 模拟盘简报（当前主要推送）

只在开盘与收盘各推一条，不逐笔推送成交。

**开盘**（09:45，持仓现价取实时价）：

```
【模拟盘·开盘】11-04

【持仓】
平安银行 000001  300股  11.59 → 11.56  -0.26%
江苏银行 600919  300股  12.49 → 12.45  -0.32%
北京银行 601169  600股   5.83 →  5.87  +0.69%
中国建筑 601668  800股   4.66 →  4.65  -0.21%
中国铁建 601186  600股   5.93 →  5.95  +0.34%

【账户】
持仓 5 只｜市值 17993｜现金 1986
总资产 19979｜累计 -0.10%
```

**收盘**（15:15，持仓现价取当日收盘价）：

```
【模拟盘·收盘】11-04

今日成交：买 0｜卖 0
盘中触发：无

【持仓】
（同上，按浮盈降序）

【账户】
总资产 19979（现金 1986 + 市值 17993）
今日 -0.15%｜累计 -0.10%
```

### 决策仪表盘
```
🎯 2026-02-08 决策仪表盘
共分析3只股票 | 🟢买入:0 🟡观望:2 🔴卖出:1

📊 分析结果摘要
⚪ 中钨高新(000657): 观望 | 评分 65 | 看多
⚪ 永鼎股份(600105): 观望 | 评分 48 | 震荡
🟡 新莱应材(300260): 卖出 | 评分 35 | 看空

⚪ 中钨高新 (000657)
📰 重要信息速览
💭 舆情情绪: 市场关注其AI属性与业绩高增长，情绪偏积极，但需消化短期获利盘和主力流出压力。
📊 业绩预期: 基于舆情信息，公司2025年前三季度业绩同比大幅增长，基本面强劲，为股价提供支撑。

🚨 风险警报:

风险点1：2月5日主力资金大幅净卖出3.63亿元，需警惕短期抛压。
风险点2：筹码集中度高达35.15%，表明筹码分散，拉升阻力可能较大。
风险点3：舆情中提及公司历史违规记录及重组相关风险提示，需保持关注。
✨ 利好催化:

利好1：公司被市场定位为AI服务器HDI核心供应商，受益于AI产业发展。
利好2：2025年前三季度扣非净利润同比暴涨407.52%，业绩表现强劲。
📢 最新动态: 【最新消息】舆情显示公司是AI PCB微钻领域龙头，深度绑定全球头部PCB/载板厂。2月5日主力资金净卖出3.63亿元，需关注后续资金流向。

---
生成时间: 18:00
```

### 大盘复盘
```
🎯 2026-01-10 大盘复盘

📊 主要指数
- 上证指数: 3250.12 (🟢+0.85%)
- 深证成指: 10521.36 (🟢+1.02%)
- 创业板指: 2156.78 (🟢+1.35%)

📈 市场概况
上涨: 3920 | 下跌: 1349 | 涨停: 155 | 跌停: 3

🔥 板块表现
领涨: 互联网服务、文化传媒、小金属
领跌: 保险、航空机场、光伏设备
```

## ⚙️ 配置说明

完整环境变量、模型渠道、通知渠道、数据源优先级、交易纪律、基本面 P0 语义和部署说明请参考 [完整配置指南](docs/full-guide.md)。

## 🖥️ Web 界面

Web 工作台提供配置管理、任务监控、手动分析、历史报告、完整 Markdown 报告、Agent 问股、回测、持仓管理、模拟盘看板（`/paper`）、选股验证（`/pick-verification`）、智能导入和浅色 / 深色主题。启动方式：

```bash
python main.py --webui
python main.py --webui-only
```

访问 `http://127.0.0.1:8000` 即可使用。认证、智能导入、搜索补全、历史报告复制、云服务器访问等细节见 [本地 WebUI 管理界面](docs/full-guide.md#本地-webui-管理界面)。

## 🤖 Agent 策略问股

配置任意可用 AI API Key 后，Web `/chat` 页面即可使用策略问股；如需显式关闭可设置 `AGENT_MODE=false`。

- 支持均线金叉、缠论、波浪理论、多头趋势、热点题材、事件驱动、成长质量、预期重估等内置策略
- 支持实时行情、K 线、技术指标、新闻和风险信息调用
- 支持多轮追问、会话导出、发送到通知渠道和后台执行
- 支持自定义策略文件与多 Agent 编排（实验性）

> Agent 具体参数、`skill` 命名兼容、多 Agent 模式和预算护栏见 [完整指南](docs/full-guide.md#本地-webui-管理界面) 与 [LLM 配置指南](docs/LLM_CONFIG_GUIDE.md)。

## 🧩 相关项目 (Related Projects)

> DSA 聚焦日常分析报告；选股实现参考 AlphaSift，AlphaEvo 用于策略验证与进化。

| 项目 | 定位 |
|------|------|
| [AlphaSift](https://github.com/ZhuLinsen/alphasift) | DSA 选股实现的参考项目 |
| [AlphaEvo](https://github.com/ZhuLinsen/alphaevo) | 策略回测与自我进化，用于验证策略规则，并通过迭代探索策略参数与组合 |

## 📬 联系与合作

<table>
  <tr>
    <td width="92" valign="top"><strong>合作邮箱</strong></td>
    <td valign="top">
      <a href="mailto:zhuls345@gmail.com">zhuls345@gmail.com</a><br>
      项目咨询、部署支持与功能扩展
    </td>
    <td align="center" rowspan="3" valign="middle" width="148">
      <a href="http://xhslink.com/m/tU520DWCKT" target="_blank"><img src="./docs/assets/xiaohongshu_tick.jpg" width="112" alt="小红书二维码"></a><br>
      <sub>扫码关注小红书</sub>
    </td>
  </tr>
  <tr>
    <td width="92" valign="top"><strong>小红书</strong></td>
    <td valign="top"><a href="http://xhslink.com/m/tU520DWCKT">欢迎关注小红书</a></td>
  </tr>
  <tr>
    <td width="92" valign="top"><strong>问题反馈</strong></td>
    <td valign="top"><a href="https://github.com/ZhuLinsen/daily_stock_analysis/issues">提交 Issue</a></td>
  </tr>
</table>

## 📄 License

[MIT License](LICENSE) © 2026 ZhuLinsen

欢迎在二次开发或引用时注明本仓库来源，感谢支持项目持续维护。

## ⚠️ 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本项目产生的任何损失负责。

---
