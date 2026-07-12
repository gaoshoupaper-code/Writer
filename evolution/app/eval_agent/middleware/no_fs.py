"""向后兼容 re-export：NoFilesystemToolsMiddleware 已迁移到共享位置（决策 S7）。

实现搬到了 app/common/middleware/no_fs.py，评估 Agent 和进化 Agent 共用。
本文件仅保留 re-export，避免已有 import 路径 `from app.eval_agent.middleware.no_fs`
全部报错——后续可统一迁移到 `from app.common.middleware.no_fs` 后删除本文件。
"""
from app.common.middleware.no_fs import NoFilesystemToolsMiddleware

__all__ = ["NoFilesystemToolsMiddleware"]
