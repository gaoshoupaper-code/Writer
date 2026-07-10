"""Trace 投影 diff 引擎（Phase 2 设计 T4 路线 Y）。

策略：projector 保持全量无状态，运行期每次全量投影后与前次快照 diff，
只推 diff 部分（node 级全量替换）。这样前端只收 patch，不再做全量重投影（消除 O(N²)）。

diff 三态（按 node_id 比对）：
  - append：前次快照没有的新 node
  - update：前次快照有但字段变了（如 running→completed、duration 回填）的 node（全量替换）
  - （delete：当前投影逻辑不删 node，暂不支持）

性能：单次 O(N) 比对（N = 当前 nodes 数）。每次投影仍 O(N)，但前端 O(N²) 根除。
"""

from __future__ import annotations

from typing import Any

from app.core.models import TraceNode


def _node_signature(node: TraceNode) -> tuple:
    """提取 node 的关键字段做变更检测（不含 started_at 等固定字段）。

    只要这些字段变了，就认为 node 有更新需要推送。
    """
    return (
        node.status,
        node.duration_ms,
        node.ended_at,
        node.error,
        node.usage.input_tokens if node.usage else None,
        node.usage.output_tokens if node.usage else None,
        node.usage.total_tokens if node.usage else None,
        node.chain_summary,
    )


class NodeSnapshotDiffer:
    """维护单个 trace 的 nodes 快照，产出增量 patch。

    用法：
        differ = NodeSnapshotDiffer()
        patch = differ.diff(latest_nodes)  # 每次投影后调
        # patch = {"appended": [...], "updated": [...]}
        # 首次调用（空快照）→ 全部 appended
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, tuple] = {}  # node_id → 字段签名
        self._initialized: bool = False

    def diff(self, current_nodes: list[TraceNode]) -> dict[str, list[TraceNode]]:
        """对比当前 nodes 与快照，返回增量 patch。

        Returns:
            {"appended": [新 node], "updated": [变更 node]}
            appended 为空 + updated 为空 = 无变化。
        """
        appended: list[TraceNode] = []
        updated: list[TraceNode] = []

        new_snapshot: dict[str, tuple] = {}

        for node in current_nodes:
            sig = _node_signature(node)
            new_snapshot[node.node_id] = sig

            if not self._initialized:
                # 首次：全部当作 append
                appended.append(node)
            else:
                old_sig = self._snapshot.get(node.node_id)
                if old_sig is None:
                    appended.append(node)
                elif old_sig != sig:
                    updated.append(node)
                # else: 无变化，跳过

        self._snapshot = new_snapshot
        self._initialized = True

        return {"appended": appended, "updated": updated}

    def reset(self) -> None:
        """重置快照（trace 切换或重连时调用）。"""
        self._snapshot.clear()
        self._initialized = False
