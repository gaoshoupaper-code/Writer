"""问题知识库（一期：历史问题—计划—结果轨迹，需求 20260731_135839）。

双层模型（DEC-01）：
  - problem_instances：不可变问题实例账本（每条来自一份 sealed 评估卷宗的 finding）
  - standard_problems：可治理标准问题库（聚合已确认实例，承载生命周期与频率）

权威存储在 evolution SQLite；语义向量/FTS 仅作派生索引（REQ-09/DEC-11）。
本期不产出经验对象、经验等级或基于经验的自动推荐（DEC-17/DEC-18）。

子模块：
  - taxonomy:     多轴分类受控词表（REQ-02/DEC-19）
  - classifier:   从 finding + 冻结证据提取多轴分类
  - repo:         5 张表的 DAL
  - ingest:       封存触发的问题实例收录（非阻塞，AC-14/AC-46）
  - current_card: 当前问题卡冻结（REQ-04.8/DEC-15）
  - retrieval:    相似检索管线（结构化→FTS→向量→RRF，REQ-04/AC-26）
"""
