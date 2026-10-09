# 每日选股与次日验证

每日从 A 股、美股各筛出 Top N 只股票落库，并在下一个交易日收盘后自动回测，
在看板上展示收益、胜率等指标，可选推送到企业微信等通知渠道。

## 1. 数据与判定口径

| 项 | 说明 |
| --- | --- |
| 选股范围 | A 股走内置快照源；美股走腾讯报价接口（`SCREENING_US_TICKERS` 可覆盖候选池，默认 50 只美股大盘股） |
| 每市场只数 | `SCREENING_PICKS_TOP_N`，默认 5；同一天同一市场只保留一份结果，重跑不会叠加 |
| 落库表 | `screening_picks`（每日入选）、`screening_verifications`（T+1 验证） |
| 入场价 | 选股当日的快照价格（`price`） |
| 验证时点 | 选股日**之后的首个交易日**收盘价（严格 T+1，不依赖运行时刻） |
| 取价数据源 | A 股走自动降级链；美股走新浪美股 K 线（`source="sina_us"`） |
| 基准 | A 股 `sh000001`（上证指数）、美股 `^GSPC`（标普 500，经新浪映射为 `.INX` 取数），可用 `SCREENING_PICKS_BENCHMARK_CN` / `SCREENING_PICKS_BENCHMARK_US` 覆盖 |
| 符合预期 | 个股 T+1 收益 > 0 且 跑赢基准当日涨幅 |
| 数据不足 | T+1 K 线尚未产生时不写库，保持待验证，下次运行自动重试 |

## 2. 环境变量

```ini
# 开启内置选股（默认关闭）
SCREENING_ENABLED=true
# 每个市场每日入选只数，默认 5
SCREENING_PICKS_TOP_N=5
# 是否推送选股 / 验证结果，默认 true
SCREENING_PICKS_NOTIFY_ENABLED=true

# 每日定时任务
SCHEDULE_ENABLED=true
SCHEDULE_TIME=18:00
# 或 SCHEDULE_TIMES=09:30,18:00

# 企业微信群机器人（也可以在 Web 设置页维护）
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<你的 key>
WECHAT_MSG_TYPE=markdown
```

美股行情不依赖 Yahoo Finance：快照走腾讯报价接口，日线走新浪美股 K 线，
两者国内可直连、无需 API key、无需代理。

**为什么不用 yfinance**：Yahoo 对机房 IP 返回 HTTP 429，国内服务器和海外 VPS
实测都会被限流，代理方案解决不了；因此 yfinance 已不在任何美股自动链路中，
仅保留 `source="yfinance"` 的手工调用入口。

美股字段口径（腾讯报价接口）：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `price` / `change_pct` / `amount` | 腾讯 [3] / [32] / [37] | 涨跌幅缺失时用昨收现算 |
| `total_mv` / `circ_mv` | 腾讯 [44] / [45] × 1e8 | 接口单位为「亿」，币种随标的报价币种（美股即美元） |
| `pe_ratio` / `turnover_rate` | 腾讯 [39] / [38] | 已用 JPM/XOM/WMT 等真实标的核对过口径 |
| `pb_ratio` / `volume_ratio` | 不提供 | 留空而非填 0；内置美股策略不对这两个字段做过滤 |

新浪美股 K 线只返回 `date/open/high/low/close/volume/amount`，且是全历史
（如 AAPL 约 1 万条），代码只保留尾部 `lookback_days` 条。

## 3. 命令行

```bash
# 每日选股：A 股 / 美股各 Top N 落库
python main.py --screen-picks
python main.py --screen-picks --picks-market cn,us --picks-top 5

# 次日验证：校验已入库选股的 T+1 表现
python main.py --verify-picks
python main.py --verify-picks --picks-market us --verify-force
```

每日调度（`--schedule` / `--serve`）在个股分析与大盘复盘收尾后会自动执行
「先验证昨日选股、再生成今日选股」，任一步失败都不会拖垮主流程。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/pick-verification/dashboard?limit_days=30` | 看板数据（按选股日聚合 + 汇总指标） |
| POST | `/api/v1/pick-verification/picks?markets=cn,us&top=5` | 手动触发每日选股 |
| POST | `/api/v1/pick-verification/verify?market=cn&force=false` | 手动触发次日验证 |

汇总指标字段：`total_picks` / `verified_picks` / `met_expectation` /
`hit_rate_pct`（符合预期占比）/ `win_rate_pct`（上涨占比）/
`avg_return_pct` / `avg_excess_return_pct` / `tracked_days`。

## 5. 看板页面

Web 端 `/pick-verification`（侧边栏「选股验证」）展示：

- 顶部汇总卡片：累计入选、已验证、命中率、胜率、平均收益、平均超额
- 按选股日分组的每日卡片：A 股 / 美股分组、排名、入场价、T+1 收盘、
  收益 / 基准 / 超额、判定标签（符合预期 / 未上涨 / 跑输基准 / 待验证）
- 手动触发按钮：执行每日选股、执行次日验证、刷新

## 6. 推送

推送复用既有通知体系（`src/notification.py`），企业微信走
`WECHAT_WEBHOOK_URL`。选股与验证各推送一条 Markdown 消息；
推送失败只记录警告，不影响落库与验证。重复执行选股不会重复推送，
只有本次确实写入新记录时才发送。

## 7. 服务器部署（Docker + cron）

### 7.1 部署形态

仓库自带一份面向「选股看板」的 compose 文件，只跑 Web/API，不跑整体 AI 分析：

```bash
docker compose -f docker/docker-compose.pickboard.yml build
docker compose -f docker/docker-compose.pickboard.yml up -d
```

| 项 | 值 |
| --- | --- |
| 容器名 | `dsa-pickboard` |
| 端口 | `8000`（配合 `WEBUI_HOST=0.0.0.0`） |
| 数据卷 | `pickboard-data` / `pickboard-logs` / `pickboard-reports` |
| 启动命令 | `python main.py --serve-only`（只服务 Web/API，选股交给宿主机 cron） |

容器内不启用 `SCHEDULE_ENABLED`，避免与 cron 重复触发。
**`.env` 改动后必须 `up -d --force-recreate`**，`restart` 不会重新注入环境变量。

### 7.2 .env 模板

```ini
TZ=Asia/Shanghai
DATABASE_PATH=/app/data/stock_analysis.db
WEBUI_ENABLED=true
WEBUI_HOST=0.0.0.0
API_PORT=8000
ADMIN_AUTH_ENABLED=false      # 公网部署建议开启；Docker 下需写入 ENV_FILE 指向的文件才生效，见 7.4

SCREENING_ENABLED=true
SCREENING_PICKS_TOP_N=5
SCREENING_PICKS_NOTIFY_ENABLED=true

WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<你的 key>
WECHAT_MSG_TYPE=markdown
WECHAT_MAX_BYTES=4000

# 容器内不跑整体分析调度
SCHEDULE_ENABLED=false
STOCK_LIST=
MARKET_REVIEW_ENABLED=false

# 行情源
TUSHARE_TOKEN=<token>

# 新闻源
TAVILY_API_KEYS=<key>
# BOCHA_API_KEYS=<key>       # 项目里 Bocha 优先级高于 Tavily

# 可选：LLM 重排（不配则走纯因子 + 本地后置评分，ranking_mode=factor）
# LLM_CHANNELS=siliconflow
# LLM_SILICONFLOW_PROTOCOL=openai
# LLM_SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
# LLM_SILICONFLOW_API_KEY=<key>
# LLM_SILICONFLOW_MODELS=deepseek-ai/DeepSeek-V4-Flash
# LITELLM_MODEL=openai/deepseek-ai/DeepSeek-V4-Flash
# LLM_TIMEOUT_SEC=180        # 默认 60 对 10 个候选偏紧，容易超时
# LLM_MAX_TOKENS=4096        # 默认 2048 会截断 ranked JSON
# LLM_RANK_WEIGHT=0.40       # 因子 60% + LLM 40%
```

新闻检索是否启用与 LLM 无关：结果富化阶段（`profile=post_rank_full`）会强制采集新闻，
只有前置排序阶段（`include_news=has_llm_config()`）才依赖 LLM 配置。

### 7.3 crontab（宿主机，时区需为 Asia/Shanghai）

```cron
# 先验证昨日选股，再生成当日选股
5  16 * * 1-5  docker exec -e WEBUI_ENABLED=false dsa-pickboard python main.py --picks-market cn --verify-picks  >> /var/log/dsa-pickboard-verify.log 2>&1
20 16 * * 1-5  docker exec -e WEBUI_ENABLED=false dsa-pickboard python main.py --picks-market cn --screen-picks >> /var/log/dsa-pickboard-picks.log 2>&1
```

要点：

- 用 `docker exec` 而非容器内调度，删容器不影响任务定义；
- 加 `-e WEBUI_ENABLED=false`，避免选股进程再去抢 8000 端口；
- 时间放在 A 股收盘（15:00）之后；`--picks-market` 可限制市场。
  美股与 A 股可共用同一份 cron（美股当日收盘价在次日北京时间上午已可取到），
  要同时跑美股就去掉 `--picks-market cn`，只想跑 A 股则保留。

### 7.4 端口与访问

容器监听 `0.0.0.0:8000`。若主机防火墙已放行、`docker-proxy` 也正常监听，
但外网仍访问不到，通常是**云厂商安全组未放行**，需要在控制台加入站规则（TCP 8000）。

#### 开启管理员鉴权（公网部署建议）

这里有一个容易踩的坑：`is_auth_enabled()` 只读 **`.env` 文件**，不看进程环境变量。
而 compose 的 `env_file:` 只是把宿主机 `.env` 注入为环境变量，**不会**在容器内生成
`/app/.env`；宿主机 `.env` 也不能单文件挂载到 `/app/.env`（Docker 会把挂载目标当
mount point，破坏配置保存时的 `os.replace` 原子替换）。所以直接在宿主机 `.env` 里写
`ADMIN_AUTH_ENABLED=true` 是**不生效**的。

按项目推荐做法，把运行期活跃配置指向 data 卷里的可写文件：

```yaml
    environment:
      - ENV_FILE=/app/data/runtime.env
```

```bash
# runtime.env 内容：ADMIN_AUTH_ENABLED=true
docker cp runtime.env dsa-pickboard:/app/data/runtime.env
docker compose -f docker/docker-compose.pickboard.yml up -d --force-recreate
curl -s http://127.0.0.1:8000/api/v1/auth/status
# {"authEnabled":true,"loggedIn":false,"passwordSet":false,"passwordChangeable":true,...}
```

生效后的拦截范围（中间件是全局的，新增接口自动纳入，无需改代码）：

| 路径 | 是否需要会话 |
| --- | --- |
| `/api/v1/*`（含 `/api/v1/pick-verification/*`） | 需要 |
| `/api/v1/auth/login`、`/api/v1/auth/status` | 豁免 |
| `/api/health`、`/docs`、`/openapi.json` | 豁免 |
| 静态页面（HTML/JS） | 不拦，但没有 Cookie 时页面调 API 会拿 401 并引导登录 |

口令在**首次访问时**由页面引导设置，哈希存在 data 卷的 `.admin_password_hash`；
忘记口令时重置：

```bash
docker exec -it dsa-pickboard python -m src.auth reset_password
```

定时任务不受影响：cron 走 `docker exec ... python main.py`，直连 SQLite，不经过 HTTP 鉴权。

其他可选加固：只绑定 `127.0.0.1` + nginx 反代加基础认证，或安全组只放行自己的 IP。

### 7.5 数据源可达性与额度（国内服务器实测参考）

| 源 | 可达 | 说明 |
| --- | --- | --- |
| 腾讯 `qt.gtimg.cn` | 是 | A 股实时行情，稳定 |
| 新浪 `hq.sinajs.cn` | 是 | 全市场快照源（代码已带 Referer） |
| 东财 `push2.eastmoney.com` | 否 | efinance / akshare_em 会失败，建议后置其优先级 |
| 腾讯美股 `qt.gtimg.cn/q=usAAPL` | 是 | 美股快照主源：批量、GBK、无 key |
| 新浪美股行情 `hq.sinajs.cn/list=gb_aapl` | 是 | 美股实时报价备选 |
| 新浪美股 K 线 `US_MinKService.getDailyK` | 是 | 美股日线与指数（`.INX` / `.IXIC` / `.DJI`） |
| Yahoo Finance | 不可用 | 机房 IP 一律 429（国内与海外 VPS 实测均如此），已从美股链路移除 |
| Tushare `api.tushare.pro` | 是 | 低积分账号 `daily` 可用；`stock_basic` / `daily_basic` 可能 1 次/小时 |
| Tavily | 是 | 免费 1000 credits/月 |
| Bocha | 可达 | 账号需有额度，否则 403 `no quota` |
| Brave / OpenAI / Gemini | 否 | 不要配置 |
| 国产 LLM（DeepSeek / 智谱 / 百炼 / Moonshot / 硅基流动） | 是 | 需要 LLM 时优先选这些 |

实测用量参考（各市场 Top5、纯因子模式）：单次选股约 9 次搜索调用，
两市场约 18 次/天，按 22 个交易日约 **396 次/月**；开启 LLM 后前置阶段会再翻约一倍。

注意：配置 `TUSHARE_TOKEN` 后，Tushare 会被自动提升为最高日线优先级
（`_determine_priority()` 中硬编码），无法通过 `TUSHARE_PRIORITY` 覆盖。

### 7.6 常见排障

| 现象 | 原因与处理 |
| --- | --- |
| 启动报 `ModuleNotFoundError: No module named 'src.data'` | 代码同步不完整（打包时误排除同名目录），补齐 `src/data/` 后重建镜像 |
| 美股快照或日线取数失败 | 先确认腾讯 `qt.gtimg.cn` 与新浪 `stock.finance.sina.com.cn` 可达；两个源都不需要 key，也不受海外网络影响 |
| 美股取数正常但日志仍出现 `Yfinance ... Rate limited` | 来自 DSA 候选上下文的实时行情链路（`data_provider/`），只影响候选附加信息，不阻塞选股 |
| `llm_ranked=false` + `invalid_response` | LLM 输出未通过覆盖率校验；检查 `LLM_MAX_TOKENS`（默认 2048 易截断） |
| `llm_ranked=false` + `timeout` | 调大 `LLM_TIMEOUT_SEC`（默认 60s 偏紧） |
| 推理模型（如 `Xing4.0-29B`）返回内容为空 | 解析器只读 `content`，推理模型的思维链会挤占输出预算，建议换非推理模型 |
| `llm_failure_reason=call_failed` | 检查 `LLM_*_BASE_URL` / key；可在容器内用 `litellm.completion` 直连验证 |
| 验证结果一直 `insufficient` | 正常：T+1 K 线尚未产生，下次运行会自动重试 |
| 重复执行选股没有新增记录 | 幂等设计：同日同市场同代码只写一次，`saved=0 skipped=N` 属正常 |
| Tushare 报 `频率超限` | 低积分账号限制；Tushare 被提到最高优先级后会频繁降级，属预期 |

### 7.7 构建注意

- 部分网络环境无法直连 Docker Hub，需要给 daemon 配 `registry-mirrors` 才能拉到基础镜像；
- 镜像构建过程会执行 `npm ci` + `npm run build` 与 `pip install -r requirements.txt`，网络差时耗时较长；
- 重建镜像后必须 `up -d --force-recreate` 才会替换运行中的容器。

### 7.8 回滚

```bash
# 停服务
docker compose -f docker/docker-compose.pickboard.yml down
# 代码回滚：换回旧目录并用旧镜像启动
mv /opt/dsa-pickboard /opt/dsa-pickboard.broken
mv /opt/dsa-pickboard.bak.<时间戳> /opt/dsa-pickboard
# 调度回滚：注释掉 crontab 中对应两行
```

数据保存在命名卷里，重建容器不会丢失。回滚前建议先备份 `pickboard-data` 卷中的 SQLite 文件。

> 选股结果仅用于研究辅助，不构成投资建议。
