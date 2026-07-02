"""versioning 包 —— 配置版本谱系 + 发版管理。

管理 harness 配置的版本快照（harness_snapshots 表）：每次发版产出一个
production 快照，旧 production 降级为 retired。负责配置发版聚合 + 通知执行端。

（trace 输入重建 increment 已归入 ingestion/——它属于 trace 数据处理，
不属于版本管理。）

模块：
  snapshot_repo.py        harness_snapshots 数据访问层 + 发布聚合
  snapshot_publisher.py   snapshot 发布器（通知执行端切换版本）
  snapshot_api.py         snapshot 查询/发布 API 路由

设计依据：.claude/md/20260701_213000_进化端重构_设计.md（improvement 重命名拆分）。
"""
