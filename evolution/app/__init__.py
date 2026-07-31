"""evolution app 包初始化。

pysqlite3 注入（问题知识库一期，需求 20260731）：
  Python 标准库 sqlite3 在发行版构建（如官方 python:3.12-slim Docker 镜像）时
  默认禁用 enable_load_extension，导致 sqlite-vec 扩展无法加载。
  pysqlite3-binary 是带扩展加载能力的等价实现，在此最早处替换 sys.modules['sqlite3']。

  必须在所有 sqlite3 使用方（core/db / langgraph-checkpoint-sqlite 等）import 之前执行。
  本文件是 app 包入口，import app.main 时最先运行。

  pysqlite3 不可用（未安装）时静默回退到标准库——问题知识库的向量扩展加载会失败，
  由 retrieval/store 层降级处理（AC-26：检索降级到结构化+FTS，不阻断 evolution 启动）。
"""
import sys as _sys

try:
    import pysqlite3 as _pysqlite3  # type: ignore[import-not-found]
except ImportError:
    # pysqlite3 未安装（如开发机）：保持标准库 sqlite3。
    # 向量扩展加载会失败并降级，不影响 evolution 其他功能。
    pass
else:
    # 标准库 sqlite3 被任何模块 import 之前，替换为 pysqlite3。
    _sys.modules["sqlite3"] = _pysqlite3
