"""MemoryRecallMiddleware — 写作前从 NWM 记忆库召回证据，注入 prompt（可进化要素）。

与 ContextAssemblerMiddleware 并存（双 middleware 兜底链，D-D5-1）：
  - ContextAssembler 总挂载：注入静态蓝图文件（outline/storyline/character）。
  - MemoryRecall 条件追加：注入动态记忆（跨章节角色状态/伏笔/关系）。
  两者前缀不同（"写作前置上下文：" vs "记忆召回："），幂等检测互不干扰，可同时注入。
  MemoryRecall 检索失败时 return None 降级（D-R5-1），ContextAssembler 仍兜底。

设计依据：NWM 设计文档 D-D4-3（middleware 改动）、D-R5-1（失败降级不中断）。

可进化部分（evolution agent 能改）：
  - query_builder（harness tools/query_builder.py）：task → writer query 构造
  - _format_packet：证据包引导语包装
  - budget / num_results：检索参数

固定部分（executor MemoryBackend 提供）：
  - retrieve 四阶段检索（causal cutoff + FTS5/vec RRF + one-hop JOIN + bounded packet）
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# 记忆证据包的前缀标签（幂等检测 + 前端识别用）
_CONTEXT_PREFIX = "记忆召回："

# 章节号提取正则（复用 events.py 的逻辑思路，不能 import 执行端）
_CHAPTER_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*章"),       # 第3章 / 第 12 章
    re.compile(r"第\s*([一二三四五六七八九十百]+)\s*章"),  # 第三章 / 第十二章
    re.compile(r"chapter[\s\-]*(\d+)", re.IGNORECASE),  # chapter-03 / Chapter 12
]

# 中文数字映射
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


class MemoryRecallMiddleware(AgentMiddleware):
    """写作前从 NWM 记忆库召回相关动态记忆，注入 prompt。

    与 ContextAssemblerMiddleware 并存（双 middleware 兜底链）：本中间件提供
    跨章节的动态记忆（角色状态/伏笔/关系），ContextAssembler 提供静态蓝图。

    Args:
        backend: MemoryBackend 实例（执行端注入，提供固定检索原语）
        group_id: 作品隔离标识（assemble 时从 owner_id + workspace 算出）
        workspace_path: workspace 路径（检测 .memory_unhealthy flag 用）
        budget_chars: 证据包字符预算（可进化参数，默认 12000）
        num_results: 检索结果数上限（可进化参数，默认 10）
    """

    def __init__(
        self,
        backend: Any,
        group_id: str,
        workspace_path: Any = None,
        budget_chars: int = 12000,
        num_results: int = 10,
        quality_callback: Callable[[dict], None] | None = None,
        query_builder: Callable[[str, int | None], str] | None = None,
    ) -> None:
        self._backend = backend
        self._group_id = group_id
        self._workspace_path = workspace_path
        self._budget_chars = budget_chars
        self._num_results = num_results
        # P4 进化闭环：检索质量埋点回调。executor 侧传入（写 trace run_meta 事件）。
        # None 时不埋点（向后兼容）。回调接收一个 dict（TraceMemoryQuality 字段）。
        self._quality_callback = quality_callback
        # Phase 5：harness 可进化的 query_builder（task→writer query）。
        # None 时用内置 _build_query（向后兼容）。
        self._query_builder = query_builder

    # ------------------------------------------------------------------
    # before_model：核心检索 + 注入
    # ------------------------------------------------------------------

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """在 LLM 调用前，从记忆库召回证据并注入 prompt。

        失败语义（D-R5-1，推翻旧"决策10硬中断"）：
          - 记忆不健康（.memory_unhealthy flag / health_check 失败）→ 降级返回 None
          - 检索异常 → 降级返回 None
          降级后 ContextAssembler 兜底全量注入（D-D5-1 双 middleware 链），不中断写作。
        """
        messages = state.get("messages", [])

        # 幂等：已注入过记忆则跳过
        if self._find_context_index(messages) is not None:
            return None

        # D-R5-1：检测 workspace 级不健康 flag → 降级（不抛错）
        if self._workspace_path is not None:
            try:
                from pathlib import Path
                flag = Path(self._workspace_path) / ".memory_unhealthy"
                if flag.exists():
                    logger.warning("记忆系统不健康（.memory_unhealthy flag），降级全量注入")
                    self._record_quality(None, "", packet=None, ok=False, error="unhealthy_flag")
                    return None
            except Exception:
                pass  # flag 检测失败不阻断

        # D-R5-1：健康检查失败 → 降级（不抛错）
        try:
            if not await self._backend.health_check():
                logger.warning("记忆健康检查失败，降级全量注入")
                self._record_quality(None, "", packet=None, ok=False, error="health_check_failed")
                return None
        except Exception as e:
            logger.warning("记忆健康检查异常，降级全量注入：%s", e)
            self._record_quality(None, "", packet=None, ok=False, error=f"health_check_error: {e}")
            return None

        # 提取 task（最后一条 HumanMessage）和章节号
        task = self._extract_task(messages)
        chapter_num = self._extract_chapter_index(task)

        # 可进化：构造检索查询
        query = self._build_query(task, chapter_num)
        if not query:
            return None

        # causal cutoff：写第 N 章时只用 ≤ N-1 章的记忆（D-R3-3，杜绝未来泄漏）
        causal_cutoff = (chapter_num - 1) if chapter_num else None

        try:
            packet = await self._backend.retrieve(
                query=query,
                group_id=self._group_id,
                causal_cutoff=causal_cutoff,
                budget_chars=self._budget_chars,
                num_results=self._num_results,
            )
        except Exception as e:
            # D-R5-1：检索失败 → 降级返回 None（不抛错中断）
            self._record_quality(chapter_num, query, packet=None, ok=False, error=str(e))
            logger.warning("记忆检索失败，降级全量注入：%s", e)
            return None

        # P4 埋点：检索成功（含完整 trace 审计，D-R1-3）
        self._record_quality(chapter_num, query, packet, ok=True)

        if not packet:
            # 图谱为空（还没入图）或无相关记忆——注入空提示，不阻断
            logger.info("记忆检索无结果（图谱可能为空），跳过注入")
            return None

        # 可进化：排版证据包
        formatted = self._format_packet(packet, chapter_num)
        if not formatted:
            return None

        return {"messages": [HumanMessage(content=formatted)]}

    # ------------------------------------------------------------------
    # 可进化部分（evolution agent 可优化）
    # ------------------------------------------------------------------

    def _build_query(self, task: str, chapter_num: int | None) -> str:
        """从 task description 构造检索查询（可进化）。

        Phase 5：优先用 harness 注入的 query_builder（assemble 时从 tools/query_builder.py 加载）。
        None 时用内置策略（task 原文截断）。
        """
        if not task:
            return ""

        # harness 可进化 query_builder 优先
        if self._query_builder is not None:
            try:
                return self._query_builder(task, chapter_num)
            except Exception:
                pass  # harness query_builder 异常时回退内置

        # 内置默认：task 原文截断（FTS5 BM25/LIKE 匹配关键词）
        return task[:2000]

    def _format_packet(self, packet: Any, chapter_num: int | None) -> str:
        """把 EvidencePacket 排版成注入文本（可进化）。

        P1 初版：在 packet.formatted 前加引导语，让 LLM 理解这是记忆召回而非指令。
        """
        if not packet.formatted:
            return ""

        chapter_hint = f"（当前写第 {chapter_num} 章）" if chapter_num else ""
        header = (
            f"{_CONTEXT_PREFIX}\n"
            f"以下是从故事图谱中检索到的相关记忆（角色状态/关系/事件/设定）{chapter_hint}。\n"
            f"请在写作时参考这些信息，保持与前文的一致性：\n\n"
        )
        return header + packet.formatted

    # ------------------------------------------------------------------
    # P4 进化闭环：检索质量埋点
    # ------------------------------------------------------------------

    def _record_quality(
        self,
        chapter_num: int | None,
        query: str,
        packet: Any | None,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """记录记忆检索质量（P4 进化闭环信号 + D-R1-3 完整检索审计）。

        通过 quality_callback 回调上报给 executor（写 trace run_meta 事件）。
        callback 为 None 时跳过（向后兼容）。

        审计字段（让 evolution 看到"召回了什么"而非仅"召回了几个"）：
          - 基础：chapter_num/query/retrieval_ok/error
          - 数量：packet tokens/nodes_count/edges_count
          - 完整审计（D-R1-3）：causal_cutoff/stage1_count/stage3_expanded/truncated/hits 摘要
        """
        if self._quality_callback is None:
            return

        # hits 摘要（只取关键字段，避免 trace 膨胀）
        hits_summary: list[dict] = []
        if packet is not None:
            for h in getattr(packet, "hits", [])[:20]:  # 限制 20 条防膨胀
                hits_summary.append({
                    "type": h.get("type"),
                    "name": h.get("name"),
                    "source_chapter": h.get("source_chapter"),
                    "via_join": h.get("via_join", False),
                })

        self._quality_callback({
            # 基础
            "chapter_num": chapter_num,
            "query": query[:200],
            "retrieval_ok": ok,
            "error": error,
            # 数量（兼容旧 TraceMemoryQuality 字段）
            "evidence_packet_tokens": getattr(packet, "token_estimate", 0) if packet else 0,
            "evidence_nodes_count": len(getattr(packet, "nodes", [])) if packet else 0,
            "evidence_edges_count": len(getattr(packet, "edges", [])) if packet else 0,
            # D-R1-3 完整检索审计
            "causal_cutoff": getattr(packet, "causal_cutoff", None) if packet else None,
            "stage1_count": getattr(packet, "stage1_count", 0) if packet else 0,
            "stage2_anchors": getattr(packet, "stage2_anchors", []) if packet else [],
            "stage3_expanded": getattr(packet, "stage3_expanded", 0) if packet else 0,
            "truncated": getattr(packet, "truncated", False) if packet else False,
            "hits": hits_summary,
        })

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _extract_task(self, messages: list) -> str:
        """提取最后一条 HumanMessage 的文本（复用 ContextAssembler 逻辑）。"""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    text = "\n".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ).strip()
                    if text:
                        return text
        return ""

    def _extract_chapter_index(self, text: str) -> int | None:
        """从文本中提取章节号。

        支持：第3章 / 第三章 / chapter-03 / Chapter 12。
        """
        if not text:
            return None

        for pattern in _CHAPTER_PATTERNS:
            match = pattern.search(text)
            if match:
                raw = match.group(1)
                if raw.isdigit():
                    return int(raw)
                return self._cn_to_int(raw)
        return None

    def _cn_to_int(self, cn: str) -> int | None:
        """中文数字转整数（支持十位，如 '二十三'→23）。"""
        total = 0
        section = 0
        for ch in cn:
            if ch in _CN_DIGITS:
                section = _CN_DIGITS[ch]
            elif ch == "十":
                total += (section if section else 1) * 10
                section = 0
        total += section
        return total if total > 0 else None

    def _find_context_index(self, messages: list) -> int | None:
        """幂等检测：已注入过记忆召回则返回索引。"""
        for i, msg in enumerate(messages):
            if (isinstance(msg, HumanMessage)
                    and isinstance(msg.content, str)
                    and msg.content.startswith(_CONTEXT_PREFIX)):
                return i
        return None


__all__ = ["MemoryRecallMiddleware"]
