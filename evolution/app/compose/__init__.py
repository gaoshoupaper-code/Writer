"""compose 包 —— Harness 配置化组装的数据层。

定义 harness 的 first-class 配置对象（HarnessConfig）及其编辑算子（edit ops）。
configuration 是权威（决策 D1a 全放 evolution），executor 只消费序列化后的 JSON。

模块：
  config.py    HarnessConfig schema + 序列化
  edits.py     apply_edits 算子（replace/insert/remove）
  bootstrap.py 从现有 assemble 硬编码生成 v1 config（一次性迁移）

设计依据：20260625_200000 设计文档。
"""
