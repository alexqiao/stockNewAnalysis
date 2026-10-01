# 第三方组件与方法声明

除明确标注的本地打包资源外，组件通过正式依赖调用，统计方法在本项目中独立实现。

## Sentence Transformers

- 项目：https://github.com/huggingface/sentence-transformers
- 许可证：Apache License 2.0
- 用途：可选的多语言标题向量与余弦相似度计算。
- 默认模型：`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`。模型文件和使用条件
  以其 Hugging Face 模型卡为准。

## Microsoft Qlib

- 项目：https://github.com/microsoft/qlib
- 许可证：MIT
- 用途：参考其量化研究评估口径，对信号分数与前向超额收益计算横截面 Rank IC 和 ICIR。
- 本项目未引入 Qlib 运行时依赖；ICIR 不做年化，并明确报告有效横截面数量。

## TradingView Lightweight Charts™ 5.0.9

Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/

The unmodified standalone production build is included at
`src/trade_news_analysis/static/lightweight-charts-5.0.9.standalone.production.js`.
The full Apache License 2.0 and upstream NOTICE are included alongside it as
`lightweight-charts-LICENSE.txt` and `lightweight-charts-NOTICE.txt`.
The stock detail page also displays TradingView attribution and its link.

- Project: https://github.com/tradingview/lightweight-charts/tree/v5.0.9
- Build source: https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.0.9.tgz
- NOTICE source: https://raw.githubusercontent.com/tradingview/lightweight-charts/v5.0.9/NOTICE
- npm tarball SHA-512: `8oQIis8jfZVfSwz8j9Z5x3O79dIRTkEYI9UY7DKtE4O3ZxlHjMK3L0+4nOVOOFq4FHI/oSIzz1RHeNImCk6/Jg==`

This library renders locally stored price data. No third-party chart service is
contacted when loading the stock detail page.
