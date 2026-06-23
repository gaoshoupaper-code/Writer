"""Writer 执行端与进化端共享的契约层。

本包是 executor 和 evolution 之间的「数据契约单一真源」：
- contracts/trace/  trace 数据 schema（两端读写 trace 的统一格式）
- contracts/api/   跨端 API 请求/响应模型（D3 trace 拉取、D7 prompt 更新通知等）
- contracts/surface_types  surface 体系类型契约（A/B/C 三层 + scope + REGISTRY）
- contracts/manifest_schema  manifest wire-format 契约（entries 结构 TypedDict）

铁律：本包不依赖 executor 也不依赖 evolution，只依赖 pydantic（trace/api 子包用）。
surface_types/manifest_schema 零三方依赖（纯类型/枚举/TypedDict）。
违反此约束会被 scripts/check_layering.py 拦截。
"""

__version__ = "0.1.0"
