# 金融新闻影响分析平台

一个面向美股的本地研究工具：持续采集新闻，把每条新闻按公司拆分为结构化影响假设，并用之后的真实行情做 1、5、20 个交易日前向验证。

> 仅供研究与教育使用，不构成投资建议。系统不执行交易，也不承诺新闻分析具有可交易优势。

## 功能

- 从 InvestorMate/yfinance、SEC RSS、Federal Reserve RSS 获取近期新闻。
- URL 指纹去重、48 小时转载聚类、保守的公司名/别名匹配。
- OpenAI-compatible 模型输出中文结构化研判：方向、置信度、需求证据、财务传导、催化剂、风险和证伪条件。
- 以分析生成后的首个可交易开盘价为基准，计算股票相对 SPY 的前向超额收益。
- FastAPI JSON API、新闻流、分析详情、自选股、验证看板和来源健康页面。
- 无 LLM 密钥时仍可采集新闻；分析会明确显示“未配置”。

## 快速开始

要求 Python 3.12。推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --extra dev
cp .env.example .env
uv run alembic upgrade head
uv run trade-news serve
```

打开 <http://127.0.0.1:8000>，API 文档位于 <http://127.0.0.1:8000/docs>。

也可以只运行一次完整流水线：

```bash
uv run trade-news ingest
```

## 模型配置

密钥只通过环境变量读取，不会写入数据库或日志：

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your-key-from-the-provider
LLM_MODEL=gpt-4o-mini
```

`LLM_BASE_URL` 可替换为实现 OpenAI Chat Completions 协议的服务地址。修改 `.env` 后重启应用。

## 常用 API

```bash
# 立即启动一次后台采集
curl -X POST http://127.0.0.1:8000/api/v1/runs/ingest

# 查询新闻和前向指标
curl 'http://127.0.0.1:8000/api/v1/news?symbol=NVDA'
curl 'http://127.0.0.1:8000/api/v1/metrics?symbol=NVDA&horizon=5'

# 查看数据源和模型状态
curl http://127.0.0.1:8000/api/v1/health
```

应用内调度器假定单进程运行。不要使用多个 Uvicorn worker；如需多实例部署，应关闭 `SCHEDULER_ENABLED`，改由外部定时任务调用采集 API 或 CLI。

## 开发与验证

```bash
uv run pytest
uv run ruff check .
uv run mypy src tests
```

带真实外部网络的测试必须显式运行：

```bash
uv run pytest -m network
```

## 数据与方法边界

- 新闻只保存标题、摘要、来源、链接与必要元数据，不默认下载全文。
- 前向验证从系统实际生成分析后开始，避免让当前模型“预测”它已经知道的历史事件。
- 三分类实际标签使用 SPY 超额收益：高于 `+0.5%` 为 bullish，低于 `-0.5%` 为 bearish，中间为 neutral。
- 样本不足 30 条时，看板明确标记“证据不足”。
- yfinance 说明其接口数据面向个人研究使用；商业使用前必须重新确认数据授权和供应商条款。

## 上游与许可证

本项目以 MIT 许可证发布，并固定依赖 [InvestorMate 0.6.0](https://github.com/siddartha19/investormate)（MIT）。InvestorMate 提供 yfinance 数据接口与通用股票对象。本项目独立实现新闻持久化、逐公司分析、转载聚类和前向验证逻辑。

设计上参考了 [stock-news-monitor](https://github.com/maverick14303/stock-news-monitor) 的“逐新闻、逐公司、持续验证”理念；由于其仓库未提供可确认的源码许可证，本项目没有复制其代码。

