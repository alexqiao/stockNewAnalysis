# 第三方组件与方法声明

本项目不复制下列仓库的大段源代码；组件通过正式依赖调用，统计方法在本项目中独立实现。

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
