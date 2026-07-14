"""MemoryRecallMiddleware — 写作前从图谱召回记忆，注入 prompt（可进化要素）。

替代 ContextAssemblerMiddleware 对 writing 子代理的全量文件注入，
改为查询条件检索——只召回与当前章节写作意图相关的有界证据包。

设计依据：设计文档 §3.2（接口契约）。

与 ContextAssemblerMiddleware 的同构点：
  - abefore_model 返回 {"messages": [HumanMessage]}，靠 add_messages reducer append
  - 幂等检测（context_prefix 前缀匹配，已注入则跳过）
  - wrap_model_call 把记忆证据重排到消息列表开头（保证 prompt caching 前缀稳定）

可进化部分（evolution agent 能改）：
  - _build_query：查询条件构造逻辑（从 task description + 章节号 → 检索查询）
  - _format_packet：证据包排版格式
  - budget / num_results 等检索参数

固定部分（MemoryBackend 提供）：
  - retrieve 的实际执行（Graphiti BM25+Vector+BFS+reranker + 时间过滤 + 截断）
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
    """写作前从 Graphiti 召回相关记忆子图，注入 prompt。

    替代 ContextAssemblerMiddleware 的全量文件注入，改为查询条件检索。

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
    ) -> None:
        self._backend = backend
        self._group_id = group_id
        self._workspace_path = workspace_path
        self._budget_chars = budget_chars
        self._num_results = num_results
        # P4 进化闭环：检索质量埋点回调。executor 侧传入（写 trace run_meta 事件）。
        # None 时不埋点（向后兼容）。回调接收一个 dict（TraceMemoryQuality 字段）。
        self._quality_callback = quality_callback

    # ------------------------------------------------------------------
    # before_model：核心检索 + 注入
    # ------------------------------------------------------------------

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """在 LLM 调用前，从图谱召回记忆并注入 prompt。

        失败语义（决策 10）：
          - 图谱不健康（health_check 失败 或 .memory_unhealthy flag 存在）→ 抛错中断
          - 检索异常 → 抛错中断（不降级到全量注入）
        """
        messages = state.get("messages", [])

        # 幂等：已注入过记忆则跳过
        if self._find_context_index(messages) is not None:
            return None

        # 决策 10：检测 workspace 级不健康 flag
        if self._workspace_path is not None:
            try:
                from pathlib import Path
                flag = Path(self._workspace_path) / ".memory_unhealthy"
                if flag.exists():
                    raise RuntimeError(
                        f"记忆图谱不可用（存在 .memory_unhealthy flag）"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass  # flag 检测失败不阻断（路径不可访问等）

        # 决策 10：FalkorDB 健康检查（TTL 缓存）
        if not await self._backend.health_check():
            raise RuntimeError("FalkorDB 健康检查失败，记忆系统不可用")

        # 提取 task（最后一条 HumanMessage）和章节号
        task = self._extract_task(messages)
        chapter_num = self._extract_chapter_index(task)

        # 可进化：构造检索查询
        query = self._build_query(task, chapter_num)
        if not query:
            return None

        try:
            packet = await self._backend.retrieve(
                query=query,
                group_id=self._group_id,
                budget_chars=self._budget_chars,
                num_results=self._num_results,
            )
        except Exception as e:
            # P4 埋点：检索失败
            self._record_quality(chapter_num, query, packet=None, ok=False, error=str(e))
            logger.error("记忆检索失败，中断写作：%s", e)
            raise RuntimeError(f"记忆检索失败：{e}") from e

        # P4 埋点：检索成功
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

        策略（P1 初版）：提取 task 中的关键实体（角色名、事件）作为查询。
        task 通常包含 "写第 N 章"、"本章事件 E0XX"、"出场角色 陈远、苏敏" 等。
        直接用 task 原文做查询（Graphiti BM25 会匹配关键词），
        前缀加章节号增强时序相关性。
        """
        if not task:
            return ""

        # 去掉过长的系统指令部分，只取核心写作意图
        # task 通常是 meta agent 的 task description，含章节目标 + 出场角色 + 事件
        query = task[:2000]  # 截断防超长查询
        return query

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
        """记录记忆检索质量（P4 进化闭环信号）。

        通过 quality_callback 回调上报给 executor（写 trace run_meta 事件）。
        callback 为 None 时跳过（向后兼容）。
        """
        if self._quality_callback is None:
            return

        self._quality_callback({
            "chapter_num": chapter_num,
            "query": query[:200],
            "evidence_packet_tokens": getattr(packet, "token_estimate", 0) if packet else 0,
            "evidence_nodes_count": len(getattr(packet, "nodes", [])) if packet else 0,
            "evidence_edges_count": len(getattr(packet, "edges", [])) if packet else 0,
            "retrieval_ok": ok,
            "error": error,
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
