"""manifest wire-format 契约（共享包，零三方依赖）。

定义 manifest entries_json 的结构——进化端构造、执行端解析，两端共用此定义，
确保 manifest 的线上传输格式有唯一真理源。

manifest 是「部署单元」：一份 manifest = 各 surface 当前 approved 版本的指针聚合。
执行端按 manifest 逐 surface 加载，装配成完整 agent。

结构（来自设计文档接口契约 + manifest_repo._build_entries）：
  {
    "surfaces": [
      {"surface_type", "surface_name", "scope", "version", "id"}, ...
    ],
    "schema_lock": {
      "c_surfaces": [{"surface_name", "scope", "version"}, ...]  # C 类版本指针（回放契约）
    }
  }

schema_lock（回放契约，决策 D3/D11）：
  - 记录该 manifest 用了哪些 C 类 surface 及版本
  - 回放老 trace 时，校验 trace 当时的 C 类版本与重放用 manifest 的 c_surfaces 一致
  - C 类改动 State schema，版本不一致 → 回放失真 → 必须拦截
"""
from __future__ import annotations

from typing import TypedDict


class SurfaceEntry(TypedDict):
    """manifest entries 里单个 surface 的指针（执行端据此拉 content 装配）。"""

    surface_type: str        # prompt / skill / stateful_middleware / ...
    surface_name: str        # 如 GoalMiddleware / meta_system_prompt
    scope: str               # 归属 subagent（meta/storybuilding/.../global）
    version: int             # 该 surface 的版本号
    id: int                  # surface_versions 表的主键 id


class CSurfaceRef(TypedDict):
    """schema_lock 里的 C 类 surface 版本指针（回放契约锁定 C 类版本）。"""

    surface_name: str        # C 类 middleware 名（如 GoalMiddleware）
    scope: str               # scope（防同名歧义）
    version: int             # C 类版本


class SchemaLock(TypedDict):
    """manifest 的回放锁：记录本 manifest 用的所有 C 类 surface 版本。"""

    c_surfaces: list[CSurfaceRef]


class ManifestEntries(TypedDict):
    """manifest entries_json 的完整结构（surfaces 指针聚合 + schema_lock 回放锁）。"""

    surfaces: list[SurfaceEntry]
    schema_lock: SchemaLock
