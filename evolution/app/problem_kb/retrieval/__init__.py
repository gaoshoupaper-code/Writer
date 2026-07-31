"""相似检索管线（REQ-04 / AC-26 / DEC-11）。

流水线：结构化过滤 → FTS5 全文召回 → 向量 KNN 重排 → RRF 融合 → 二级排序。
逐级降级：vec 不可用→仅 FTS+结构化；全失败→空结果 + 降级标记（不得表述为"无历史问题"）。

子模块：
  - embedder: 智谱 embedding-3 客户端（带调用计数，AC-39）
  - store:    sqlite-vec + FTS5 派生索引
  - search:   混合检索
  - matcher:  候选归并生成（收录时触发）
"""
