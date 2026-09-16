# 项目优化计划

## 本轮已落地

- **结论可操作**：默认 5 日、对照 1/20 日；增加持仓状态、行动候选、阻断原因、催化与退出核验条件。
- **X 信息接入**：按公司/事件/主题关联，展示原帖与待核验事项、作者分歧和采集缺口；失败筛选可重试。
- **防止陈旧结论误导**：信号和报价时效门槛、独立人工价格确认时间、撤回证据后中性快照；超过当前时间 5 分钟的事件暂停作为行动证据。
- **读性能**：SQL 仅加载每只证券最新快照，移除详情页全部历史快照预加载；不删除历史数据。
- **界面体积**：主题从全量下拉改为按输入搜索，一次最多 30 条；折叠长公司简介，优先展示行动。
- **一致性**：首页、证券页和 API 使用同一规则；补充规则、迁移、查询与端到端回归测试。

## 下一步优先级

| 优先级 | 优化项 | 解决的问题 | 完成标准 |
| --- | --- | --- | --- |
| P0 | 恢复 X 浏览器采集 | 有账号但无帖子时无法使用博主信息 | 每个启用账号有成功采集时间；原帖能沿关联路径到证券页 |
| P1 | 保存行动快照并前向验证 | 当前指标只检验新闻排序，无法证明买卖规则有效 | 保存当时持仓、规则版本、价格和证据；计入费用、滑点、最大回撤，按市场/周期做样本外验证 |
| P1 | 仓位与风险预算 | 当前只能给方向，不能给买卖数量 | 用户填写成本、仓位比例、单笔风险及最大敞口；情景结果可复算，无自动交易 |
| P1 | 结构化证据核验 | 分数高仍可能主要依赖推断 | 明示公告/报道/个人观点，核验订单、盈利兑现时间、失效事实；只在一手依据满足后升级证据等级 |
| P2 | 行情与估值适用性 | 工作日近似、陈旧财年和非盈利企业限制判断 | 接各交易所日历与行情时间；区分短期催化和年度估值；为不适用 PE 的资产提供合适模型 |
| P2 | X 作者与观点质量评估 | 热度不能代表判断质量 | 按作者、主题、预测周期跟踪可验证主张，控制重复与幸存者偏差；样本不足不做作者权重 |

先收集可复现的行动样本，再调整阈值。不要根据少量命中案例提高信号权重。

## 本地运行检查（2026-09-15）

检查时有 11 个自选股、7 个 X 账号、0 条 X 帖子，X 自动采集关闭；历史账号均无成功采集记录。
本轮单账号抓取返回 `net::ERR_HTTP_RESPONSE_CODE_FAILURE`，另一次 X 首页导航成功，说明需要
继续检查目标博主页访问与登录会话。不能将这次连通性结果解释为采集已恢复。

新增字段需 `uv run alembic upgrade head`。已有持仓默认未知；人工价格需重新保存才会有独立
确认时间。启动后的使用顺序是：填写持仓 → 更新价格与盈利假设 → 看行动与阻断条件 → 核验原文。

## 改动文件

- 接口与数据：`src/trade_news_analysis/api.py`、`models.py`、`schemas.py`；迁移 `alembic/versions/e2b7c4a91d60_holding_status_and_price_confirmation.py`。
- 服务：`src/trade_news_analysis/services/judgment.py`、`social_context.py`、`x_posts.py`、`pe_analysis.py`、`scoring.py`、`opportunities.py`。
- 页面：`src/trade_news_analysis/templates/_decision.html`、`index.html`、`security.html`、`watchlist.html`、`x_posts.html`；样式 `src/trade_news_analysis/static/style.css`。
- 验证：`tests/conftest.py`、`test_api.py`、`test_decisions_api.py`、`test_judgment.py`、`test_migrations.py`、`test_pe_analysis.py`、`test_scoring.py`、`test_opportunity_queries.py`、`test_social_context.py`、`test_x_posts.py`。
- 说明：`README.md`、`OPTIMIZATION_PLAN.md`。
