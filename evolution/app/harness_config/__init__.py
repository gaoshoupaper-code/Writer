"""harness_config 包 —— HarnessConfig 配置化组装的数据层。

定义 harness 的 first-class 配置对象（HarnessConfig）及其编辑算子（edit ops）。
configuration 是权威（决策 D1a 全放 evolution），executor 只消费序列化后的 JSON。

模块：
  config.py    HarnessConfig schema + 序列化 + 校验
  edits.py     apply_edits 算子（replace/insert/remove）
  bootstrap.py 从现有 assemble 硬编码生成 v1 config（一次性迁移）
  class_ref.py middleware 类名 → 源码路径解析（要素展示用）

（git 操作已归入 core/git_ops.py——它是通用 git 工具，不属于配置层。）

设计依据：20260625_200000 设计文档 + 20260701_213000 重构设计（compose 拆分）。
"""
