# DEPRECATED — v1 harness（Phase 6 surface 体系已取代）

> **状态：已废弃（2026-06-23，Phase 6 T5.3）**
> **替代：`surface_versions` 表 + `manifest_loader.assemble`（surface 体系）**

本目录的 `WriterHarnessV1` / 各 `SubagentHarness` 子类是 Phase 1-4 的"整体 harness"
装配路径（`_assemble_via_harness`）。Phase 6 重构后，装配知识已迁移到：

- **数据**：`evolution.surface_versions` 表（A/B/C 三类 surface，由 `migrate_to_surface.py` 从本目录代码迁移）
- **部署快照**：`evolution.harness_manifests` 表（manifest 聚合各 surface 版本）
- **装配**：`executor.app.platform.harness.manifest_loader.assemble`（manifest → AssembledManifest）

## 何时移除

`_assemble_via_harness` 路径仍保留（`writer_use_harness` 开关控制），供：
1. Phase 6 端到端验证期间的等价性对照（T5.2）
2. manifest 拉取失败时的降级后备（`_assemble_via_manifest` 内部降级到此）

**移除条件**（三个全满足）：
- [ ] `writer_use_manifest` 在生产稳定运行 ≥ 2 周
- [ ] 端到端等价性验证通过（manifest 装配输出 = harness 装配输出）
- [ ] 降级后备不再需要（evolution 高可用）

满足后：删本目录 + 移除 `_assemble_via_harness` + 删 `writer_use_harness` 开关。

## 不要在此目录新增功能

任何 harness 改动应通过 evolution surface 体系（创建 surface 版本 → A/B → manifest 发布），
不再直接改这里的代码。本目录冻结，仅供读取/对照。
