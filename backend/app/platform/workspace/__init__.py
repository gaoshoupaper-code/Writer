"""platform.workspace 子包：workspace 元数据 + 路径管理（DD7c）。

BaseWorkspaceStore 提供 owner 限定的元数据 CRUD + 路径解析。
产物读写（read_outline / read_images / ...）下沉到各 domain 的 artifact store。

Phase 1 阶段预留位置，物理迁移在 Phase 2 完成。
"""
