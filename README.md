# 股票事件研究平台

一个覆盖 A 股、港股和美股的本地研究工具。系统将多篇新闻先聚合为市场事件，再推导产业链主题和候选股票，最后把多个事件汇总为股票的 1、5、20 个交易日综合研判。

> 新闻是证据，不是结论。系统仅供研究与教育使用，不执行交易，也不承诺涨停或收益。

## 核心链路

~~~text
新闻 → 事件簇 → 产业链主题 → 候选股票 → 股票综合研判 → 前向验证
~~~

- 新闻通过 URL 指纹、中文/英文标题相似度和 72 小时时间窗聚合为事件。
- 模型第一阶段提取需求变化、主题、产业链角色和候选公司。
- 候选必须与证券主数据唯一匹配；无法验证的公司只显示为“待核实”，不进入排名。
- 模型第二阶段评估财务传导和八个研究维度，代码计算透明的 0–100 弹性分。
- 同一事件只贡献一次；多来源报道提高证据质量，不重复抬高股票信号。
- 股票页聚合同向和反向事件，并展示净方向、置信度与冲突度。
- 验证看板记录 Top-K 候选的超额收益、方向命中率和排序相关性。

## 数据源与降级

- 配置 TUSHARE_TOKEN 时，优先同步 A/港/美股证券主数据和日线行情。
- TUSHARE_NEWS_ENABLED=true 仅应在账户具备新闻权限时启用。
- 安装 data 扩展后，可在无 Token 时用 AKShare 补充 A 股证券列表和宽口径财经新闻。
- 港美股免费降级使用 InvestorMate/yfinance；SEC、Federal Reserve RSS 始终作为独立事件源。
- 配置 Finnhub Key 后可按美股自选股补充公司新闻；Key 只通过请求头发送。
- SEC EDGAR 默认追踪美股自选股的 8-K、6-K、10-Q 和 10-K，不抓取申报全文。
- GDELT 默认用单次批量请求按自选股公司名查询全球新闻，作为宏观和供应链补充。
- /api/v1/health 显示各能力和市场覆盖。覆盖不足时，“没有采到新闻”不能解释成“没有影响”。

## 快速开始

要求 Python 3.12，推荐使用 uv：

~~~bash
uv sync --extra dev --extra data
cp .env.example .env
uv run alembic upgrade head
uv run trade-news serve
~~~

打开 <http://127.0.0.1:8000>，API 文档位于 <http://127.0.0.1:8000/docs>。

运行一次完整流水线：

~~~bash
uv run trade-news ingest
~~~

应用内调度器假定单进程运行。多实例部署应关闭 SCHEDULER_ENABLED，由外部任务触发采集 API 或 CLI。

## 模型与数据配置

凭证只从环境变量读取，不写入数据库或日志：

~~~dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your-key-from-the-provider
LLM_MODEL=gpt-4o-mini
# 仅在供应商支持 thinking 扩展时设置
# LLM_THINKING=disabled

# 可选
TUSHARE_TOKEN=
TUSHARE_NEWS_ENABLED=false
AKSHARE_ENABLED=true
FINNHUB_API_KEY=
FINNHUB_NEWS_ENABLED=false
SEC_EDGAR_ENABLED=true
GDELT_NEWS_ENABLED=true
GDELT_BASE_URL=https://api.gdeltproject.org/api/v2/doc/doc
NEWS_LOOKBACK_HOURS=48
~~~

LLM_BASE_URL 可以替换为兼容 OpenAI Chat Completions 的服务。修改 .env 后需重启应用。

### 可选语义事件聚类

默认事件聚类使用标题词元 Jaccard 相似度。需要处理中英文改写标题时，可安装
Sentence Transformers 扩展：

~~~bash
uv sync --extra semantic
~~~

然后启用本地 CPU 模型：

~~~dotenv
SEMANTIC_CLUSTERING_ENABLED=true
SEMANTIC_CLUSTERING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
SEMANTIC_CLUSTERING_THRESHOLD=0.82
~~~

模型在第一次需要语义比较时懒加载，并可能从 Hugging Face 下载模型文件，因而会增加
首次运行时间、磁盘和内存占用。依赖缺失、下载失败或推理异常时，采集会继续运行并自动
退回词法聚类；状态可在 `/api/v1/health` 查看。该能力只影响之后采集的新闻，不重写历史事件。

Finnhub 需先申请免费 API Key，再设置 `FINNHUB_NEWS_ENABLED=true`。SEC EDGAR 和
GDELT 不需要 Key。`HTTP_USER_AGENT` 建议包含可联系邮箱，以符合 SEC 公平访问要求。
如当前代理网络在 GDELT TLS 握手阶段超时，可显式改用官方无密钥 HTTP 入口：

~~~dotenv
GDELT_BASE_URL=http://api.gdeltproject.org/api/v2/doc/doc
~~~

该降级仅适用于不携带凭证的公开新闻查询；网络恢复后应切回 HTTPS。

## Telegram 机会日报

系统可以在每周一至周五 08:30（Asia/Shanghai）向一个 Telegram 私人会话发送
5 日信号 Top 5。日报沿用网页的跨市场机会口径：先选出美股 7 项和 A 股 3 项，
再按综合分排序并发送前 5 项。每项机会会附上最新一篇支持新闻的原始来源链接。

1. 在 Telegram 中打开 `@BotFather`，发送 `/newbot` 并保存生成的 Token。
2. 打开新 Bot，发送 `/start`。Bot 不能在用户开始会话前主动发送私信。
3. 先只配置 Token，再读取最近会话：

~~~dotenv
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=your-bot-token
~~~

~~~bash
uv run trade-news telegram-chats
~~~

4. 将输出的私人会话 ID 写入 `.env`，预览并测试发送：

~~~dotenv
TELEGRAM_CHAT_ID=your-private-chat-id
TELEGRAM_DIGEST_TIMEZONE=Asia/Shanghai
TELEGRAM_DIGEST_HOUR=8
TELEGRAM_DIGEST_MINUTE=30
TELEGRAM_DIGEST_HORIZON=5
TELEGRAM_DIGEST_LIMIT=5
# 可选；必须是手机可访问的部署地址，不能使用 127.0.0.1
PUBLIC_BASE_URL=
~~~

~~~bash
uv run trade-news telegram-digest --dry-run
uv run trade-news telegram-digest
~~~

5. 测试成功后设置 `TELEGRAM_ENABLED=true` 并重启服务。内置调度器关闭时，
定时日报也会关闭，但仍可使用 CLI 手动发送。

服务运行后，可以直接在配置的私人聊天中发送：

~~~text
/digest  立即发送当前跨市场机会日报
/help    查看可用指令
~~~

指令默认每 5 秒轮询一次，并将处理进度保存到
`./data/telegram_update_offset`，避免应用重启后重复执行旧指令。Bot 使用
`getUpdates` 接收指令，因此不能同时配置 Telegram webhook。

Token 只应保存在本地环境变量中。程序不会将 Token 写入数据库或日志。

### 从公网打开本地详情页

本机已安装并认证 ngrok 时，可以将 `127.0.0.1:8000` 转换为手机可访问的
HTTPS 地址。项目提供的启动脚本会在入口启用 Basic Auth，避免将写入类 API
直接暴露到公网；密码只进入权限受限的临时文件，ngrok 停止后会自动删除。

在另一个 zsh 终端中设置访问凭据并启动隧道：

~~~bash
export TRADE_NEWS_TUNNEL_USER=trade-news
read -s "TRADE_NEWS_TUNNEL_PASSWORD?Tunnel password (at least 8 characters): "
export TRADE_NEWS_TUNNEL_PASSWORD
echo
./scripts/start-ngrok.sh
~~~

复制 ngrok 输出中 `Forwarding` 后的 `https://...ngrok.app` 地址，写入 `.env`：

~~~dotenv
PUBLIC_BASE_URL=https://your-domain.ngrok.app
~~~

重启 `uv run trade-news serve` 后，Telegram 日报中的详情链接会使用该公网地址。
手机首次打开时需要输入上面设置的用户名和密码。隧道进程必须保持运行；按
`Ctrl-C` 即可停止公网访问。

## 常用 API

~~~bash
# 立即采集、聚合和分析
curl -X POST http://127.0.0.1:8000/api/v1/runs/ingest

# 跨市场机会榜
curl 'http://127.0.0.1:8000/api/v1/opportunities?market=A&horizon=5'

# 证券、主题与事件
curl 'http://127.0.0.1:8000/api/v1/securities?q=Apple'
curl http://127.0.0.1:8000/api/v1/themes
curl http://127.0.0.1:8000/api/v1/events/1

# 原始新闻证据和 Top-K 验证
curl 'http://127.0.0.1:8000/api/v1/news?symbol=AAPL'
curl 'http://127.0.0.1:8000/api/v1/metrics?market=US&horizon=5&top_k=10'
~~~

自选股在数据库中始终关联稳定的 `security_id`；API 既接受 `security_id`，也接受可选市场加精确代码/公司名称。后端会先唯一解析，本地主数据没有但已明确市场的标准代码会通过 yfinance 核验后再保存。

## 评分与验证

弹性分由需求确定性 20、财务传导 20、业务纯度 15、规模弹性 15、市场忽视 10、新颖度与未定价 10、证据质量 5、验证速度 5 组成，并扣除最多 20 分风险项。

股票综合信号按事件方向、弹性分、模型置信度和时间衰减聚合。1、5、20 日信号分别以相同交易日数作为半衰期；正反事件同时存在时冲突度上升。

前向验证从信号生成后所在交易所的首次可交易开盘开始。除方向命中率和平均超额收益外，
看板按市场、周期和信号生成时间计算完整证券横截面的 Spearman Rank IC，并汇总未年化
Rank ICIR；少于三个有效证券的横截面不参与计算。逻辑基准为：

- A 股：CN_CSI300
- 港股：HK_HSI
- 美股：US_SPY

具体行情代码由数据适配器转换。A 股只有数据源提供每日涨停价时才记录“触及涨停”，系统不估计涨停概率。

## 开发验证

~~~bash
uv run pytest
uv run ruff check .
uv run mypy src tests
~~~

带真实网络的测试必须显式运行：

~~~bash
uv run pytest -m network
~~~

## 数据边界

- 默认只保存标题、摘要、来源、链接和必要元数据，不下载新闻全文。
- 模型提出的公司必须经过本地证券主数据解析，无法唯一匹配的候选不会进入排名。
- 历史信号以快照持久化，避免用后来出现的新闻重写过去预测。
- 样本不足 30 条时，看板明确标记“证据不足”。
- yfinance 与 AKShare 的免费接口仅适合研究用途；商业使用前必须确认数据授权和供应商条款。

## 许可证

项目使用 MIT 许可证，并固定依赖 InvestorMate 0.6.0（MIT）。可选语义扩展和分析方法
来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
