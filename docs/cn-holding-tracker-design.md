# 国内 A 股股票分析与持仓跟踪系统 —— 设计说明

## 0. 定位与边界

在现有 `daily_stock_analysis` 之上收敛出的**纯 A 股**闭环：每日推荐 → 次日复盘 → 持仓跟踪 → 用回测数据反向调参。

| 项 | 约束 | 落地方式 |
| --- | --- | --- |
| 市场 | 仅 `cn` | 调度只传 `--picks-market cn`；服务层 `market != "cn"` 直接报错 |
| 排除美股 | 不加载美股策略、不调用美股数据源 | 策略按 `market_scope == ["cn"]` 过滤；`snapshot_us.py` 与 `--picks-market us` 不进入调度 |
| 排除港股 | 同上 | 不配置 HK universe |
| 数据表 | 不新增美股字段 | `screening_picks.market` 恒为 `cn` |

美股相关代码（`snapshot_us.py`、`us_blue_chip_momentum.yaml`）**保留但不启用**，便于将来切换；调度层不引用即不产生任何美股请求。

## 1. 总体架构

```
每日盘后 16:20
[行情快照] → [硬过滤] → [因子打分] → [政策加成] → [去重/分散] → [Top N]
     ↑            ↑                       ↑             │
 [日线库]    [策略 YAML]          [宏观政策事件]        ↓
                                              [screening_picks]
                                                      │
                                                   建立跟踪
                                                      ↓
                                             [tracked_positions]
                                                      ↓
每日盘前 09:10
                                        [持仓复盘评分卡]
                                                      ↓
                        [holding_reviews] → 持有/减仓/离场 → 推送 + 看板
```

三条独立流水线，互不阻塞：

| 流水线 | 时点 | 产物 |
| --- | --- | --- |
| 政策线 | 08:45 | `macro_events` |
| 选股线 | 16:20 | `screening_picks` + `tracked_positions` |
| 复盘线 | 09:10 / 16:40 | `holding_reviews` / `screening_verifications` |

## 2. 每日选股标准

### 2.1 漏斗（按序执行，逐步收窄）

| 步骤 | 条件 | 参考剩余 |
| --- | --- | --- |
| S0 全市场 | 沪深主板 + 创业板（可选） | ~5500 |
| S1 风险排除 | 非 ST/*ST、非退市整理、非停牌、上市 ≥ 60 交易日 | ~4500 |
| S2 流动性 | 成交额 ≥ 1 亿 且 总市值 ≥ 50 亿 | ~1800 |
| S3 价格可买 | 3 ≤ price ≤ 15 元（1 手 ≤ 1500 元） | ~1355（实测） |
| S4 波动约束 | -4% ≤ 当日涨跌幅 ≤ +8.5% | ~1200 |
| S5 估值 | 0 < PE_TTM ≤ 80 且 0 < PB ≤ 8 | ~800 |
| S6 去重 | 近 10 个交易日未推荐、非在跟踪持仓 | ~700 |
| S7 分散 | 同一申万一级行业 ≤ 2 只 | ~690 |
| S8 排序 | 因子总分 + 政策加成 取 Top N（默认 5） | 5 |

阈值集中在策略 YAML 中，**按版本号管理**（如 `balanced_alpha@v3`），便于回测归因。

### 2.2 因子权重（总分 100）

| 因子 | 权重 | 计算要点 | 数据来源 |
| --- | --- | --- | --- |
| value | 0.22 | PE / PB 的行业分位 | 快照 |
| quality | 0.20 | ROE、毛利率、资产负债率（缺失按中性 50） | 快照 / 基本面 |
| momentum | 0.18 | 20 日收益、MA 多头排列 | `stock_daily` |
| liquidity | 0.15 | 成交额分位 | 快照 |
| stability | 0.12 | 波动率、最大回撤 | `stock_daily` |
| size | 0.08 | 市值分位（中等市值优先） | 快照 |
| activity | 0.05 | 量比、换手率 | 快照 |

### 2.3 政策加成（±10，封顶）

匹配条件：`macro_events.sectors` 与个股行业有交集，且 `event_date ∈ [T-5, T]`。

| 事件方向 | 强度 1–2 | 强度 3–5 |
| --- | --- | --- |
| positive（受益） | +2 | +5 |
| negative（受限/风险） | -3 | -8 |

同一行业命中多条事件时累加后 **clamp 到 [-10, +10]**，并把命中的事件 id 写入 `policy_hits`，保证可解释。

### 2.4 去重与分散（现有系统缺失，本设计核心新增）

| 规则 | 参数 | 说明 |
| --- | --- | --- |
| 冷却窗 | `COOLDOWN_TRADING_DAYS=10` | 同一 code 在最近 N 个**交易日**被推荐过 → 排除 |
| 同日唯一 | code 唯一 | 已有约束 |
| 行业上限 | `MAX_PER_INDUSTRY=2` | 申万一级 |
| 持仓排除 | `tracked_positions.status ∈ {holding, reduced}` → 排除 | 避免推荐已持有的票 |
| 冷却豁免 | 正向政策强度 ≥ 4 且 分数提升 ≥ 15 | 允许打破冷却以捕捉新催化，记 `dedupe_note` |
| 无永久黑名单 | — | 好票不应永久消失，靠冷却窗而非禁用 |

### 2.5 输出契约

```json
{
  "pick_date": "2026-11-02",
  "market": "cn",
  "strategy": "balanced_alpha@v3",
  "picks": [
    {
      "code": "600016",
      "name": "民生银行",
      "rank": 1,
      "entry_price": 3.56,
      "final_score": 78.4,
      "factor_scores": {"value": 86.1, "quality": 72.0, "momentum": 61.2},
      "policy_hits": [
        {"event_id": 12, "direction": "positive", "strength": 4, "delta": 5}
      ],
      "dedupe_note": "cooldown_ok",
      "reason": "低估值 + 稳增长政策受益 + 流动性达标"
    }
  ]
}
```

## 3. 复盘判断依据

### 3.1 两层复盘

| 层 | 时点 | 回答的问题 | 落表 |
| --- | --- | --- | --- |
| L1 命中复盘 | T+1 收盘后 | 「这次推荐对不对」→ 策略胜率 | `screening_verifications` |
| L2 持仓复盘 | 每交易日 09:10 | 「现在还该不该拿着」→ 操作建议 | `holding_reviews` |

分层的意义：L1 是**策略质量度量**（调参依据），L2 是**当前决策输出**（操作依据）。混在一起会让胜率统计被持仓状态污染。

### 3.2 持仓复盘评分卡（0–100）

| 维度 | 权重 | 判据与分档 |
| --- | --- | --- |
| 趋势 | 30 | 收盘 > MA5 > MA10 > MA20 → 30；收盘 > MA20 但未多头排列 → 18；跌破 MA20 → 0 |
| 相对强度 | 20 | 近 5 日超额收益：≥ +5% → 20；0~+5% → 12；< 0 → 4 |
| 量能 | 15 | 量比 1.2~3 且价涨 → 15；量比 > 3 且价跌 → 0（放量下跌）；其他 → 8 |
| 风险 | 20 | 无预警 → 20；距入场 -5%~-8% → 10；跌破 MA20 → 5；触发硬性离场 → 0 |
| 政策/消息 | 15 | 无负面 → 15；行业中性 → 10；命中负面事件 → 0 |

总分 = Σ 各维度；同时输出各维度分与命中理由（`reasons`）。

### 3.3 三档建议

| 建议 | 触发条件 |
| --- | --- |
| `hold` 继续持有 | 总分 ≥ 70 且未触发硬性离场 |
| `reduce` 减仓观察 | 45 ≤ 总分 < 70，或命中任一软性预警 |
| `exit` 建议离场 | 总分 < 45，或命中任一硬性离场 |

**硬性离场**（不受总分影响，任一命中即 `exit`）：

1. 浮亏 ≥ 8%
2. 收盘跌破 MA20 且当日成交额 > 5 日均额 × 1.5（放量破位）
3. 命中重大负面事件：立案调查 / 退市风险 / 业绩预减 ≥ 50% / 大股东减持 ≥ 1%
4. 持有满 10 个交易日且超额收益 < -3%（时间成本止损）

**软性预警**（记入 `reasons`，触发 `reduce`）：

- 连续 2 日缩量下跌
- 距入场价最高涨幅 ≥ +15% 后回撤 ≥ 8%（利润保护）
- 所属行业指数近 5 日跑输上证 ≥ 3%

### 3.4 输出契约

```json
{
  "code": "600016",
  "review_date": "2026-11-03",
  "holding_days": 1,
  "close": 3.61,
  "return_pct": 1.40,
  "excess_return_pct": 0.62,
  "scores": {"trend": 30, "relative": 12, "volume": 8, "risk": 20, "policy": 15, "total": 85},
  "advice": "hold",
  "reasons": [
    "收盘价站上 MA5/MA10/MA20",
    "近 5 日跑赢上证 0.62 个百分点",
    "未命中负面政策事件"
  ],
  "hard_exit": false
}
```

## 4. 数据库表结构

### 4.1 直接复用（不改结构）

| 表 | 用途 | 说明 |
| --- | --- | --- |
| `stock_daily` | 行情 + MA5/MA10/MA20 + 量比 + 涨跌幅 | 复盘评分卡直接读，无需另建行情表 |
| `screening_runs` | 整次运行原始响应 | 审计 / 复现 |
| `screening_verifications` | L1 命中复盘 | 已实现 |
| `intelligence_sources` / `intelligence_items` | 资讯源与条目 | 宏观政策的原始输入 |
| `portfolio_accounts` / `portfolio_trades` / `portfolio_positions` | 实盘账本 | 需精确成本/盈亏时接入 |

### 4.2 扩展 `screening_picks`（加列，不重建表）

```sql
ALTER TABLE screening_picks ADD COLUMN factor_scores    TEXT;  -- JSON：各因子得分
ALTER TABLE screening_picks ADD COLUMN policy_hits      TEXT;  -- JSON：命中的政策事件
ALTER TABLE screening_picks ADD COLUMN dedupe_note      TEXT;  -- 去重/豁免说明
ALTER TABLE screening_picks ADD COLUMN strategy_version TEXT;  -- 如 balanced_alpha@v3
```

### 4.3 新增表

**(1) `macro_events` —— 宏观政策事件**

```sql
CREATE TABLE macro_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_date DATE         NOT NULL,
  title      VARCHAR(300) NOT NULL,
  source     VARCHAR(100) NOT NULL,   -- 新华社 / 央行 / 发改委 / RSS 名称
  url        VARCHAR(1000),
  level      VARCHAR(16)  NOT NULL,   -- central / ministry / local
  category   VARCHAR(24)  NOT NULL,   -- monetary / fiscal / industry / regulatory
  direction  VARCHAR(8)   NOT NULL,   -- positive / negative / neutral
  strength   SMALLINT     NOT NULL,   -- 1-5
  sectors    TEXT,                    -- JSON 数组：["银行","建筑装饰"]
  summary    TEXT,
  raw_text   TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (event_date, title, source)
);
CREATE INDEX ix_macro_event_date      ON macro_events(event_date);
CREATE INDEX ix_macro_event_direction ON macro_events(direction, event_date);
```

**(2) `tracked_positions` —— 跟踪标的（跨日，一个代码一条活跃记录）**

```sql
CREATE TABLE tracked_positions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  code             VARCHAR(10) NOT NULL,
  name             VARCHAR(64),
  market           VARCHAR(8)  NOT NULL DEFAULT 'cn',
  source_pick_id   INTEGER REFERENCES screening_picks(id),
  source_date      DATE NOT NULL,
  entry_date       DATE,
  entry_price      REAL,
  quantity         REAL NOT NULL DEFAULT 0,
  avg_cost         REAL,
  status           VARCHAR(12) NOT NULL DEFAULT 'watching',  -- watching/holding/reduced/closed
  exit_date        DATE,
  exit_price       REAL,
  exit_reason      VARCHAR(32),
  last_review_date DATE,
  last_advice      VARCHAR(8),
  last_score       REAL,
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uix_tracked_active_code
  ON tracked_positions(code)
  WHERE status IN ('watching','holding','reduced');
CREATE INDEX ix_tracked_status ON tracked_positions(status, updated_at);
```

> 部分唯一索引在 SQLite ≥ 3.8 可用；若目标库不支持，改由应用层保证「一个代码只有一条活跃记录」。

**(3) `holding_reviews` —— 每日复盘明细**

```sql
CREATE TABLE holding_reviews (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id       INTEGER NOT NULL REFERENCES tracked_positions(id),
  code              VARCHAR(10) NOT NULL,
  review_date       DATE NOT NULL,
  holding_days      INTEGER NOT NULL,
  close             REAL,
  return_pct        REAL,
  excess_return_pct REAL,
  trend_score       REAL,
  relative_score    REAL,
  volume_score      REAL,
  risk_score        REAL,
  policy_score      REAL,
  total_score       REAL,
  advice            VARCHAR(8) NOT NULL,   -- hold / reduce / exit
  reasons           TEXT,                  -- JSON 数组
  hard_exit         BOOLEAN NOT NULL DEFAULT 0,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (position_id, review_date)
);
CREATE INDEX ix_holding_review_date ON holding_reviews(review_date, advice);
```

### 4.4 保留策略

| 表 | 保留期 | 理由 |
| --- | --- | --- |
| `macro_events` | 永久 | 政策回溯与归因需要 |
| `screening_picks` | 永久 | 胜率统计的分母 |
| `holding_reviews` | 24 个月 | 明细足够；更长周期用聚合视图 |
| `stock_daily` | 永久（现状） | 回测基础 |

## 5. 模块与输入输出

| 模块 | 输入 | 处理 | 输出 | 落地 |
| --- | --- | --- | --- | --- |
| 政策采集 | `intelligence_sources`（RSS/公告） | 分类、行业映射、强度打分、去重 | `macro_events` | 新增 `macro_policy_service.py`，复用 `intelligence_service.py` |
| 政策加成 | 候选池 + 近 5 日 `macro_events` | 行业交集 + 方向/强度 → delta | 每只 ±10 内加分 | 新增 `policy_scorer.py`，接在 pipeline L1 之后 |
| 行情准备 | 交易日历 | 快照 + 日线补全 | `stock_daily` / 内存快照 | 复用现有 |
| 选股主流程 | 快照 + 策略 YAML | 硬过滤 → 因子 → 政策 → 去重 → Top N | `screening_picks` | 复用 `pipeline`，新增 `dedupe_filter.py` |
| 推荐落库 + 建跟踪 | Top N | 写 picks，同时建 `tracked_positions(watching)` | 两表 | 改造 `screening_verification_service` |
| L1 命中复盘 | picks + T+1 日线 | 涨跌 + 超额 | `screening_verifications` | 已实现 |
| L2 持仓复盘 | `tracked_positions` + 日线 + `macro_events` | 评分卡 → 三档建议 | `holding_reviews`，回写 `last_advice` | 新增 `holding_review_service.py` |
| 通知 | picks / reviews | Markdown 渲染 | 企业微信 | 复用 `notification.py` |
| 看板 | 上述四张表 | 聚合 | `/cn-tracker` 页面 | 复用前端框架，新增页面 |

## 6. 调度时序（仅交易日，Asia/Shanghai）

| 时间 | 任务 | 说明 |
| --- | --- | --- |
| 08:45 | `--collect-macro` | 采集政策事件并去重入库 |
| 09:10 | `--review-holdings` | 基于上一交易日收盘做持仓复盘 + 早间建议推送 |
| 15:10 | `--sync-daily` | 当日日线落库（供次日复盘/验证） |
| 16:20 | `--screen-picks --picks-market cn` | 选股 → picks + tracked_positions + 推送 |
| 16:40 | `--verify-picks --picks-market cn` | L1 命中复盘（昨日 picks 的 T+1） |

> 09:10 使用昨日收盘价，避免盘中噪声导致建议反复。若需要盘中提醒，可另加 11:20 / 14:30 两次异动检查（可选，不改变状态机）。

## 7. 与现有代码的映射

| 能力 | 现状 | 改造类型 |
| --- | --- | --- |
| A 股选股引擎 | 已有（`src/services/screening/` + 策略 YAML） | 复用 |
| 策略 YAML | `balanced_alpha` 等 | 增加 `version`，便于归因 |
| 快照 / 日线 | 已有（sina / tencent / tushare） | 复用 |
| picks / verification | 已实现 | 扩展列 |
| 美股（快照/日线/策略） | 已实现 | **从调度移除**，代码保留不启用 |
| 资讯（RSS/情报） | `intelligence_service` | 复用为政策输入 |
| 持仓账本 | `portfolio_*` | 可选接入 |
| 通知 | `notification.py` | 复用 |

## 8. 关键取舍

1. **政策做成事件表，而不是让 LLM 直接打标签**：可审计、可回溯、可回测；LLM 仅作分类辅助，结果落表后不再依赖模型在线。
2. **去重用冷却窗而非永久黑名单**：永久黑名单会让策略僵化（优质标的再也不出现）；10 个交易日冷却既避免反复推荐同一只，又保留二次介入机会。
3. **复盘分两层**：L1 衡量策略质量（调参），L2 输出当前操作（持有/减仓/离场）。合在一起会让胜率统计被持仓状态污染。
4. **跟踪表不复用 `portfolio_*`**：`portfolio_*` 是完整账本（流水 + FIFO），适合实盘核对；推荐跟踪只需轻状态机。要实盘时通过 `portfolio_trades` 记录，由现有 replay 生成持仓，用 `code` 关联。

## 9. 分期落地

| 期 | 内容 | 验收标准 |
| --- | --- | --- |
| P0 | 排除美股调度 + 冷却去重 + 表结构与迁移 | 连续 5 个交易日推荐无重复；四张新表可用 |
| P1 | 政策采集 + 政策加成 | `macro_events` 每日有新增；选股 `reason` 含政策命中 |
| P2 | 持仓复盘 + 三档建议 + 推送 | 每日 09:10 出建议；看板可查历史复盘 |
| P3 | 看板扩展 + 用 20 日数据校准阈值 | 胜率/平均超额可读；阈值有回测依据 |

## 10. 待确认参数（括号内为建议默认）

| 参数 | 默认 | 可选范围 |
| --- | --- | --- |
| 每日推荐数 `N` | 5 | 3–10 |
| 冷却交易日 | 10 | 5 / 10 / 20 |
| 单行业上限 | 2 | 1–3 |
| 价格区间 | 3–15 元 | 按资金规模调整 |
| 硬性止损 | -8% | -5% ~ -10% |
| 时间止损 | 10 个交易日且超额 < -3% | 5–20 日 |
| 三档阈值 | 70 / 45 | 需回测校准 |
| 政策加成上限 | ±10 | ±5 ~ ±15 |

## 11. 已实现能力与使用方式

本节记录 P0–P3 的落地结果，与上文设计一一对应。

### 11.1 新增数据表

| 表 | 落库内容 | 迁移方式 |
| --- | --- | --- |
| `macro_events` | 政策事件（层级/类别/方向/强度/影响行业） | `create_all` 自动建表 |
| `tracked_positions` | 跟踪标的（watching/holding/reduced/closed） | 同上 |
| `holding_reviews` | 每日复盘（5 维评分 + 三档建议 + 理由） | 同上 |
| `screening_picks` 扩展列 | `factor_scores` / `policy_hits` / `dedupe_note` / `strategy_version` | 自愈式 `ALTER TABLE`（幂等） |

### 11.2 命令

```bash
# 1) 政策事件采集（08:45）
python main.py --collect-macro [--macro-lookback-days 3]

# 2) 持仓复盘 + 早间建议推送（09:10）
python main.py --review-holdings [--no-notify]

# 3) 日线同步入库（15:10）
python main.py --sync-daily

# 4) 每日选股（16:20，仅 A 股）
python main.py --screen-picks --picks-market cn --picks-top 5

# 5) 次日命中复盘（16:40）
python main.py --verify-picks --picks-market cn

# 人工标记持仓状态：CODE:STATUS[:价格[:数量[:日期]]]
python main.py --mark-position 600016:holding:3.56:1000
python main.py --mark-position 600016:closed:3.90
# 补记历史交割日（不填则记当天）
python main.py --mark-position 600016:closed:3.90:0:2026-11-02
```

### 11.3 选股链路（实测数据）

```
5500 只 → 硬过滤 → 候选池 30 只 → 清洗行业 → 政策加成(±10)
        → 冷却窗 10 日 + 行业上限 2 + 持仓排除 + 豁免上限 2
        → 重排取 5 只 → 落库 + 建跟踪 + 推送
```

一次实测日志：

```
选股去重: input=30 kept=13 dropped=17 exempted=2
每日选股完成: saved=5 skipped=0 tracked=5
```

### 11.4 复盘链路（实测数据）

```
持仓复盘完成: positions=5 reviews=5 errors=0
advice={'hold': 3, 'reduce': 1, 'exit': 1}
持仓复盘推送完成: success=True
```

评分卡分档示例（真实输出）：

| 标的 | 总分 | 建议 | 关键理由 |
| --- | --- | --- | --- |
| 江苏银行 | 85 | hold | 站上 MA5/MA10/MA20，近 5 日跑赢上证 1.7pct |
| 中国铁建 | 24 | exit | 跌破 MA20 + 放量下跌 1.5 倍 + 跑输 6.2pct |

### 11.5 定时任务（宿主机 cron，仅 A 股）

```cron
45 8 * * 1-5 ... --collect-macro
10 9 * * 1-5 ... --review-holdings
10 15 * * 1-5 ... --sync-daily
20 16 * * 1-5 ... --screen-picks --picks-market cn
40 16 * * 1-5 ... --verify-picks --picks-market cn
```

### 11.6 已知限制

1. **行业来源**：A 股全市场快照（新浪/腾讯）不含行业字段，因此优先用 `INDUSTRY_MAP_FILES` 指向的映射文件填充（见 11.7），缺失时退回证券简称关键词推断，仍推断不到则按「行业未知」处理、不参与行业上限计数。
2. **政策为规则化粗筛**：搜索 → 关键词分类 → 来源/信号双重过滤（只保留 `gov.cn` 与主流财经媒体，且标题或摘要需含政策类词汇）。当前口径偏保守（一次采集约 4 条），宁可少也不引入噪音；建议每周人工抽查一次 `macro_events`。
3. **选股必须在收盘后运行**：盘前/盘中新浪快照的 `trade` 字段为 0，全市场只有个位数标的有效，会报「本次未产出候选」。cron 放在 16:20 不受影响。
4. **跟踪表不等于实盘账本**：`tracked_positions` 只做推荐跟踪与状态机；真实成本、盈亏、FIFO 仍由 `portfolio_*` 负责，两者用 `code` 关联。
5. **候选池上限需运行时抬高**：选股管线的候选池常量默认为 12（面向 LLM 排序设计，不读环境变量），本模块在 `run_daily_picks` 里运行时改为 30，否则去重+分散后凑不满 5 只。

### 11.7 行业映射数据源

行业字段影响两件事：`MAX_PER_INDUSTRY` 行业分散、政策加成的行业匹配。

```
Tushare stock_basic.industry  ──┐
                                ├→ data/screening/industry_map.csv
新浪行业板块（48 个板块）      ──┘            ↓
                                    INDUSTRY_MAP_FILES
                                              ↓
                        选股管线 enrich_industry_concepts 写入快照 industry
                                              ↓
                        fill_missing_industries 兜底（名称关键词）
```

| 数据源 | 覆盖 | 说明 |
| --- | --- | --- |
| Tushare `stock_basic` | 5577 只（近全市场） | 细分行业最准（「钢加工」「乳制品」「通信设备」）；接口 1 次/小时，映射文件按 **7 天**复用 |
| 新浪「新浪行业」 | 约 2326 只 | Tushare 不可用时兜底，无 key 无配额，但分类较粗（「金融行业」含银行/保险/证券） |
| 名称关键词 | 增量 | 仅对前两者未覆盖的标的生效；已移除「科技/控股/云」等过宽词 |

实测（2026-11-02，走新浪兜底）：候选 5 只中 4 只填上行业（中国建筑 → 建筑建材、平安银行 → 金融行业），剩余 1 只由名称推断兜底。

刷新方式：`python main.py --sync-daily` 顺带刷新（7 天内跳过），返回结构带 `industry.reason`（`ok` / `fresh` / `all_sources_failed`）。

### 12.7 自动交易 bot（盘中真实执行）

`paper_trading_service` 是「收盘后回放开盘成交」（`--paper-run`），
`paper_bot_service` 是**盘前挂单 + 盘中按实时价成交**，成交判断发生在事前。

| 时间 | 命令 | 动作 |
| --- | --- | --- |
| 09:25 | `--paper-plan` | 生成订单：买 = 昨日选股（受剩余仓位与预算约束），卖 = 今日复盘 `exit`/`reduce` |
| 09:35 | `--paper-execute` | 拉实时价成交；买单高开 >3% 放弃；跌停卖单跳过；现金不足自动降手数 |
| 10:00 / 11:00 / 13:00 / 14:00 | `--paper-watch` | 实时价检查止损止盈，触发即清仓 |
| 15:15 | `--paper-settle` | 收盘估值 + 当日/累计盈亏 |

**风控参数**：

| 参数 | 值 | 说明 |
| --- | --- | --- |
| 硬止损 | **-8%** | 与复盘硬性离场同阈值，但盘中更早执行 |
| 硬止盈 | **+20%** | 到点落袋 |
| 移动止盈 | **浮盈 +10% 激活，从持仓期最高价回落 5% 清仓** | 保住已实现利润 |

止损 8% / 止盈 20% 对应盈亏比 1:2.5；移动止盈在 +10% 之后接管，避免"涨上去又坐回来"。

**推送只有两条**（不逐笔推送，也不推未成交订单）：

开盘简报（只报持仓与账户，成交明细只在收盘汇总）：

```
【模拟盘·开盘】11-03

【持仓】
江苏银行 600919  300股  12.52 → 12.72  +1.60%
中国建筑 601668  800股  4.62 → 4.65  +0.65%
北京银行 601169  600股  5.83 → 5.87  +0.69%
平安银行 000001  300股  11.63 → 11.54  -0.77%
交通银行 601328  400股  8.72 → 8.57  -1.72%

【账户】
持仓 5 只｜市值 17948｜现金 2058
总资产 20006｜累计 -0.25%
```

开盘的持仓现价取**实时价**（09:35 那一刻），收盘的取**当日收盘价**。

收盘简报（持仓按浮盈降序）：

```
【模拟盘·收盘】11-03

今日成交：买 1｜卖 0
盘中触发：无

【持仓】
江苏银行 600919  300股  12.54 → 12.72  +1.46%
中国建筑 601668  800股  4.62 → 4.65  +0.73%
北京银行 601169  600股  5.83 → 5.87  +0.71%
平安银行 000001  300股  11.63 → 11.54  -0.75%
交通银行 601328  400股  8.72 → 8.57  -1.75%

【账户】
总资产 20006（现金 2058 + 市值 17948）
今日 +0.28%｜累计 +0.03%
```

两条合计约 650 字节，远低于企业微信 4096 字节上限。未成交订单只落 `paper_orders.note`，不推送。

**手动立即建仓**：`--paper-trade-now` 用「当前最新信号」按实时价立即成交，用于人工干预。
与 cron 流程的差别只有三点：

| 项 | cron 流程 | 手动建仓 |
| --- | --- | --- |
| 信号日 | 严格早于当日（T+1） | 允许当天 |
| 信号时效 | 超过 10 天跳过 | 不限制 |
| 高开阈值 | >3% 放弃 | 不套用 |

剩余仓位与现金约束**仍然生效**，不会透支。

**与回放模式的关系**：cron 使用 bot 的四个命令；`--paper-run` 保留用于手动补跑历史交易日。
三者共用同一账户，`dedup_hash` 前缀分别为 `bot:` 与 `paper:`，不会互相覆盖。

> 清空模拟盘时必须连 `holding_reviews` 一起删：残留的复盘建议会把新持仓当成旧标的卖掉
> （实测踩过：重建后 5 只持仓被旧 `exit` 建议卖掉 1 只）。

## 12. 模拟盘（纸上交易）

### 12.1 定位

用真实推荐结果驱动一套虚拟账户，验证「选股 + 复盘」在扣掉手续费之后到底能不能赚钱，而不必先投入真金白银。

**复用既有账本，不另建表**：

| 用途 | 复用对象 |
| --- | --- |
| 账户 | `portfolio_accounts`（名称固定为「A股模拟盘」） |
| 入金 | `portfolio_cash_ledger`（direction=in，日期固定为开户日） |
| 成交 | `portfolio_trades`（含 `fee` 佣金 / `tax` 印花税） |
| 持仓 | `portfolio_positions`（FIFO replay） |
| 净值 | `portfolio_daily_snapshots.total_equity` |

### 12.2 默认参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| 初始资金 | **20000 元** | `--paper-initial-cash` 可改 |
| 仓位数量 | 跟随 `SCREENING_PICKS_TOP_N`（默认 5，上限 20） | 每只预算 = 总权益 / 仓位数；改选股只数后模拟盘自动跟随 |
| 最小交易单位 | 100 股（1 手） | 不足 1 手不买 |
| 佣金 | 万 2.5，单笔最低 5 元 | 买卖双向 |
| 印花税 | 千 1 | 仅卖出 |
| 成交价 | **信号次日开盘价** ± 0.1% 滑点 | 复盘 09:10 出建议 → 09:30 开盘挂单；选股 T 日盘后 → T+1 开盘买入 |
| 无法成交 | 一字涨停买不进、一字跌停卖不出、停牌 | 用「最高价 == 最低价 + 涨跌方向」判定，不猜涨跌停幅度（主板 10% / 双创 20% / ST 5%） |

### 12.3 每日流程（16:50）

```
1) 卖出：读当日 09:10 产生的复盘建议
   advice=exit   → 清仓
   advice=reduce → 卖出一半（整手；不足一手直接清仓）
   成交价 = 当日开盘价；一字跌停 / 停牌则不成交，留待下一交易日
2) 买入：取「严格早于当日」的最近一个选股日作为信号
   信号距当日超过 10 天则跳过（避免执行过期信号）
   成交价 = 当日开盘价；一字涨停 / 停牌则不成交
   每只预算 = 总权益 / 5，向下取整到手
3) 同步跟踪表：把实际持仓登记为 `holding`（清仓时置为 `closed`）
   否则复盘只覆盖「当日选股」，模拟盘持有的历史标的拿不到持有建议
4) 收盘估值
   写 portfolio_daily_snapshots，得到当日 total_equity
```

幂等：同一账户 + 同一交易日 + 同一标的 + 同一方向只下单一次（`dedup_hash`），重复运行不会重复买卖。

### 12.4 命令

```bash
python main.py --paper-init [--paper-initial-cash 20000]   # 建户 + 入金（幂等）
python main.py --paper-run                                  # 建仓/调仓 + 估值
python main.py --paper-report                               # 概览（净值/持仓/成交）
```

### 12.5 实测输出（2026-11-02）

```
模拟盘交易日完成: date=2026-11-02 buys=5 sells=0 equity=19975.00 cash=1891.00
```

```
模拟盘日报 · 2026-11-02

总资产 19975.00 元（初始 20000 元，收益 -25.00 元 / -0.12%）
持仓市值 18084.00 元｜可用现金 1891.00 元

【持仓】
- 000001 300 股｜成本 11.56｜现价 11.54｜浮盈 -5.00
- 600919 300 股｜成本 12.74｜现价 12.72｜浮盈 -5.00
- 601077 500 股｜成本 7.15｜现价 7.14｜浮盈 -5.00
- 601186 600 股｜成本 5.87｜现价 5.86｜浮盈 -5.00
- 601668 800 股｜成本 4.66｜现价 4.65｜浮盈 -5.00
```

说明：建仓当日浮盈恒为 `-5.00 元/只`（收盘价成交，成本被 5 元最低佣金抬高的差额），总资产较初始 -25 元即 5 笔佣金。这是模拟盘的正常起点，不是数据错误。

### 12.6 局限

1. **开盘价回放**：系统在 16:50 用当日已产生的开盘价撮合，等价于「开盘挂单成交」，但不模拟盘中滑点与部分成交；一字板与停牌已排除。
2. **无 T+1 限制**：同日买入的标的理论上当日可卖。实际不会发生（选股 16:20、复盘 09:10，且买入信号固定取前一交易日），但跨假期的极端情况仍需注意。
3. **不含分红送转**：`portfolio_corporate_actions` 未接入，长周期持有会低估收益。
4. **不区分实盘与模拟**：模拟盘账户与真实账户共用 `portfolio_*`，通过 `name=「A股模拟盘」` 区分，不要用真实券商同名账户。
5. **成本口径**：已持仓标的的复盘收益按模拟盘实际成本（含佣金）计算，与日报一致；未持仓的关注标的仍用推荐价，其"浮盈"是假设值。
