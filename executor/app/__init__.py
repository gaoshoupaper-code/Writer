"""executor app 包初始化。

pysqlite3 注入（NWM 记忆系统，2026-07-17）：
  Python 标准库 sqlite3 在发行版构建时默认禁用 enable_load_extension，
  导致 sqlite-vec 扩展无法加载（conn.enable_load_extension 不存在）。
  pysqlite3-binary 是带扩展加载能力的等价实现，在此最早处替换 sys.modules['sqlite3']。

  必须在所有 sqlite3 使用方（langgraph-checkpoint-sqlite / 记忆系统 store 等）import 之前执行。
  本文件是 app 包入口，import app.main 时最先运行，早于 app.platform.* 任何子模块。

  pysqlite3 不可用（未安装）时静默回退到标准库——记忆系统的向量扩展加载会失败，
  由 memory 层降级处理（D-R5-1：降级全量注入不中断写作），不阻断 executor 启动。
"""
import sys as _sys

try:
    import pysqlite3 as _pysqlite3  # type: ignore[import-not-found]
except ImportError:
    # pysqlite3 未安装（如开发机未装）：保持标准库 sqlite3。
    # 记忆系统的 sqlite-vec 扩展加载会失败并降级，不影响 executor 其他功能。
    pass
else:
    # 标准库 sqlite3 被任何模块 import 之前，替换为 pysqlite3。
    # 后续 `import sqlite3` 拿到的实际是 pysqlite3，具备 enable_load_extension。
    _sys.modules["sqlite3"] = _pysqlite3
