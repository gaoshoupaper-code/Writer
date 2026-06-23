"""Agent 包装配层（Phase 7 包化重构）。

harness = 自包含的 Agent 包（evolution/harnesses/current/）。执行端同进程 import 包，
调 package.assemble(ctx) 装配完整 agent。运行时值（model/backend/checkpointer/trace/
workspace）由 RuntimeContext 注入。

Phase 6 的 manifest 装配（manifest_loader + AssembleContext + surface 体系）已被
Phase 7 包化取代：
  - 旧：从 evolution DB 拉 manifest 指针 → 解析 surface content → 装配
  - 新：import 包目录 → package.assemble(ctx) → 包内完成全部装配

harness 这个词现为历史遗留命名（platform/harness 目录名保留以减少 import 变动），
实际承载的是 package_loader（包加载）+ ab_runner（A/B 隔离执行）。

核心导出：
  - package_loader：Agent 包加载（生产路径，同进程 import current）
  - ab_runner：A/B 子进程隔离执行（解压快照 → worker 子进程）
"""
from app.platform.harness.package_loader import load_current_package

__all__ = ["load_current_package"]
