"""数据集管理模块（数据闭环设计 Phase A）。

分层数据集 golden/growing 的元数据管理 + golden revision 锁定。

- repo.py：dataset_meta 表 CRUD
- revision.py：golden revision（git commit hash）锁定机制
- api.py：管理 API（列表 / 升级 growing→golden / 查 revision）
"""
