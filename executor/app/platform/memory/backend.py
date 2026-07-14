"""MemoryBackend — 记忆系统固定基础设施核心。

封装 Graphiti 的检索/入图/健康检查，对 harness middleware 隐藏图库细节。
四个固定原语：retrieve / add_episode / ingest_storybuilding / health_check。

设计依据：设计文档 §3.1（接口契约）。

四阶段检索的落地（NWM 论文蓝图）：
  阶段1 Causal Restriction（因果截止）→ 本模块用 reference_time 上限过滤
  阶段2 Hybrid Node Ranking（BM25 + Vector）→ Graphiti search_ 默认 SearchConfig
  阶段3 One-Hop Graph Expansion（图扩展）→ Graphiti BFS 默认 max_depth=3
  阶段4 Bounded Packet Assembly（有界证据包）→ 本模块做字符预算截断

关键认知：Graphiti 的 search_ 默认配置（BM25 + cosine_similarity + BFS + cross_encoder
reranker）已覆盖 NWM 四阶段检索的阶段 2-4。我们只需补阶段 1（因果截止）和阶段 4（截断）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphiti_core_falkordb import Graphiti

logger = logging.getLogger(__name__)

# health_check 缓存 TTL（秒）。决策 §3.5：30 秒缓存避免每章写作的网络往返。
_HEALTH_CACHE_TTL = 30.0


@dataclass
class EvidencePacket:
    """检索返回的有界证据包。

    formatted 是排版好的文本，直接注入 writing 子代理的 prompt。
    其他字段供 trace 埋点（P3 进化闭环）用。
    """

    nodes: list[dict] = field(default_factory=list)  # 命中的图节点摘要
    edges: list[dict] = field(default_factory=list)  # 命中的关系边摘要
    formatted: str = ""  # 排版好的文本（≤ budget_chars），注入 HumanMessage
    token_estimate: int = 0  # 估算 token 数（中文约 1 字 ≈ 1.5 token）
    query_used: str = ""  # 实际查询字符串（trace 用）

    def __bool__(self) -> bool:
        """空证据包（无内容）判 False，middleware 据此决定是否注入。"""
        return bool(self.formatted.strip())


class MemoryBackend:
    """记忆系统固定基础设施。封装 Graphiti 调用，对 harness 隐藏图库细节。

    生命周期：进程单例（通过 client.get_memory_backend() 懒加载创建）。
    线程安全：Graphiti 客户端内部有连接池，支持并发调用。
    """

    def __init__(self, graphiti_client: "Graphiti") -> None:
        self._client = graphiti_client
        # health_check 缓存：(is_healthy, timestamp)
        self._health_cache: tuple[bool, float] | None = None
        self._indices_ready = False

    # ------------------------------------------------------------------
    # 原语 1：四阶段检索（memory_recall middleware 调用）
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        group_id: str,
        *,
        causal_cutoff: datetime | None = None,
        budget_chars: int = 12000,
        num_results: int = 10,
    ) -> EvidencePacket:
        """四阶段检索的固定执行。

        阶段 1 Causal Restriction：causal_cutoff 非 None 时，只检索该时间点之前的事实。
            利用 Graphiti 的 search_filter 按 valid_at 上限过滤（杜绝未来 scaffold 泄漏）。
        阶段 2-3 Hybrid + One-Hop：Graphiti search_ 默认配置（BM25+Vector+BFS+reranker）。
        阶段 4 Bounded Assembly：字符预算截断，保证注入 token 有界。

        失败语义：抛异常（由 middleware 的 health_check 兜底 + try/except 捕获）。
        """
        try:
            # 延迟 import（graphiti 可能未安装）
            from graphiti_core_falkordb.search.search_filters import SearchFilters

            # 阶段 1：Causal Restriction —— 构造时间过滤
            search_filter = None
            if causal_cutoff is not None:
                # Graphiti 的 SearchFilters 支持 max_date 限制事实有效期上限
                search_filter = SearchFilters(max_date=causal_cutoff)

            # 阶段 2-3：Graphiti 默认四阶段检索（BM25 + cosine + BFS + cross_encoder）
            results = await self._client.search_(
                query=query,
                group_ids=[group_id],
                search_filter=search_filter,
            )

            # 阶段 4：Bounded Assembly —— 组装有界证据包
            packet = self._assemble_evidence(results, query, budget_chars)
            logger.debug(
                "记忆检索完成：query=%s... group=%s nodes=%d edges=%d chars=%d",
                query[:40], group_id,
                len(packet.nodes), len(packet.edges), len(packet.formatted),
            )
            return packet

        except Exception as e:
            logger.error("记忆检索失败：query=%s... group=%s error=%s", query[:40], group_id, e)
            raise

    def _assemble_evidence(
        self, results: Any, query: str, budget_chars: int
    ) -> EvidencePacket:
        """阶段 4：把 Graphiti 检索结果组装成有界证据包。

        Graphiti search_ 返回 SearchResults（含 nodes/edges/episodes/communities）。
        按"角色→关系→地点→事件"的叙事优先级排版，字符预算截断。
        """
        nodes_out: list[dict] = []
        edges_out: list[dict] = []
        sections: list[str] = []
        current_chars = 0

        # ── 节点（角色/地点/故事线/事件等）──
        for node in getattr(results, "nodes", []) or []:
            labels = getattr(node, "labels", []) or []
            name = getattr(node, "name", "") or ""
            summary = getattr(node, "summary", "") or ""
            if not name:
                continue
            label_str = "/".join(labels) if labels else "Entity"
            block = f"【{label_str}】{name}"
            if summary:
                block += f"：{summary}"
            block += "\n"

            if current_chars + len(block) > budget_chars:
                break
            sections.append(block)
            current_chars += len(block)
            nodes_out.append({"label": label_str, "name": name, "summary": summary[:100]})

        # ── 关系边（带双时间戳的事实）──
        for edge in getattr(results, "edges", []) or []:
            fact = getattr(edge, "fact", "") or ""
            if not fact:
                continue
            valid_at = getattr(edge, "valid_at", None)
            invalid_at = getattr(edge, "invalid_at", None)
            # 有效期标注：当前为真 vs 已失效
            time_tag = ""
            if valid_at:
                time_tag += f"（自 {valid_at.strftime('%Y-%m-%d')}"
                if invalid_at:
                    time_tag += f" 至 {invalid_at.strftime('%Y-%m-%d')}，已失效"
                else:
                    time_tag += " 至今"
                time_tag += "）"

            block = f"- {fact}{time_tag}\n"
            if current_chars + len(block) > budget_chars:
                break
            sections.append(block)
            current_chars += len(block)
            edges_out.append({"fact": fact[:100], "valid_at": str(valid_at), "invalid_at": str(invalid_at)})

        formatted = "".join(sections)
        # token 估算：中文约 1 字 ≈ 1.5 token
        token_estimate = int(len(formatted) * 1.5)

        return EvidencePacket(
            nodes=nodes_out,
            edges=edges_out,
            formatted=formatted,
            token_estimate=token_estimate,
            query_used=query,
        )

    # ------------------------------------------------------------------
    # 原语 2：章节正文入图（Causal Publish Flow）
    # ------------------------------------------------------------------

    async def add_episode(
        self,
        name: str,
        episode_body: str,
        reference_time: datetime,
        group_id: str,
        *,
        entity_types: dict[str, Any] | None = None,
        edge_types: dict[str, Any] | None = None,
    ) -> None:
        """章节正文作为 episode 入图，Graphiti 用 schema 引导 LLM 抽取实体/关系。

        entity_types/edge_types 由 harness 包的 narrative_schema 提供（可进化）。
        None 时用 Graphiti 默认抽取（无叙事学类型引导，质量较低）。
        """
        from graphiti_core_falkordb.nodes import EpisodeType

        await self._ensure_indices()

        await self._client.add_episode(
            name=name,
            episode_body=episode_body,
            source_description=f"章节正文：{name}",
            reference_time=reference_time,
            source=EpisodeType.text,
            group_id=group_id,
            entity_types=entity_types,
            edge_types=edge_types,
        )
        logger.info("章节入图完成：name=%s group=%s", name, group_id)

    # ------------------------------------------------------------------
    # 原语 3：storybuilding 蓝图层结构化入图
    # ------------------------------------------------------------------

    async def ingest_storybuilding(
        self,
        workspace_path: Path,
        group_id: str,
        *,
        storyline_parser: Any = None,
        story_calendar: Any = None,
    ) -> None:
        """storybuilding 产物结构化入图。

        storyline_parser 是 harness tools 模块对象（含 parse_storyline/parse_threads/
        parse_characters 函数 + StoryCalendar 类）。story_calendar 是已实例化的
        StoryCalendar。两者由 harness 包提供（可进化），本方法只负责调 Graphiti 建节点/边。

        如果 storyline_parser 为 None（harness 还没实现），降级到 episode 级入图
        （全量文件喂入，Graphiti 自行抽取）。

        为什么解析器通过参数传入而非 import：
        harness 包是 git 独立仓库，executor 运行时动态 import。解析器模块由调用方
        （ingestion.py 管道）从 harness 包加载后传入，保持 executor 不硬依赖 harness。
        """
        await self._ensure_indices()

        if storyline_parser is not None:
            # 结构化路径：解析 → 建节点/边
            await self._ingest_structured(
                workspace_path, group_id, storyline_parser, story_calendar
            )
        else:
            # 降级路径：全量文件作为 episode 喂入，Graphiti 默认抽取
            await self._ingest_episodes(workspace_path, group_id)

    async def _ingest_structured(
        self,
        workspace_path: Path,
        group_id: str,
        storyline_parser: Any,
        story_calendar: Any,
    ) -> None:
        """结构化入图：解析 markdown → 建 EntityNode + EntityEdge。"""
        from graphiti_core_falkordb.nodes import EntityNode

        # ── 角色（用 storyline_parser.parse_characters）──
        characters = []
        if hasattr(storyline_parser, "parse_characters"):
            characters = storyline_parser.parse_characters(workspace_path)
        for char in characters:
            node = EntityNode(
                name=char.name,
                group_id=group_id,
                summary=char.summary,
                labels=["Character"],
            )
            node.attributes = {
                "aliases": char.aliases,
                "role_type": char.role_type,
            }
            await node.generate_name_embedding(self._client.embedder)
            await node.save(self._client.driver)

        # ── 故事线 ──
        if hasattr(storyline_parser, "parse_threads"):
            for thread in storyline_parser.parse_threads(workspace_path):
                node = EntityNode(
                    name=f"{thread.thread_id}-{thread.name}",
                    group_id=group_id,
                    summary=thread.global_arc[:500] if thread.global_arc else "",
                    labels=["Thread"],
                )
                node.attributes = {
                    "thread_type": thread.thread_type,
                    "status": thread.status,
                }
                await node.generate_name_embedding(self._client.embedder)
                await node.save(self._client.driver)

        # ── 事件（StoryNode）──
        events = storyline_parser.parse_storyline(workspace_path)
        for evt in events:
            ref_time = (
                story_calendar.to_datetime(evt.story_time)
                if story_calendar and evt.story_time
                else datetime.now()
            )
            node = EntityNode(
                name=f"{evt.event_id}-{evt.title}",
                group_id=group_id,
                summary=evt.description[:500] if evt.description else "",
                labels=["StoryNode"],
            )
            node.attributes = {
                "event_type": evt.event_type,
                "event_group": evt.event_group,
                "story_time": evt.story_time,
            }
            await node.generate_name_embedding(self._client.embedder)
            await node.save(self._client.driver)

        logger.info(
            "storybuilding 结构化入图完成：group=%s characters=%d events=%d",
            group_id, len(characters), len(events),
        )

    async def _ingest_episodes(self, workspace_path: Path, group_id: str) -> None:
        """降级路径：全量文件作为 episode 喂入。"""
        from graphiti_core_falkordb.nodes import EpisodeType

        files_to_ingest = [
            ("storyline.md", "故事核心与故事线"),
            ("worldview.md", "世界观设定"),
        ]
        # storyline/*.md
        storyline_dir = workspace_path / "storyline"
        if storyline_dir.exists():
            for f in sorted(storyline_dir.glob("*.md")):
                files_to_ingest.append((f"storyline/{f.name}", f"故事线：{f.stem}"))
        # character/*.md
        char_dir = workspace_path / "character"
        if char_dir.exists():
            for f in sorted(char_dir.glob("*.md")):
                files_to_ingest.append((f"character/{f.name}", f"角色：{f.stem}"))

        for rel_path, desc in files_to_ingest:
            full_path = workspace_path / rel_path
            if not full_path.exists():
                continue
            content = full_path.read_text(encoding="utf-8")
            if not content.strip():
                continue
            await self._client.add_episode(
                name=rel_path.replace("/", "-").replace(".md", ""),
                episode_body=content,
                source_description=desc,
                reference_time=datetime.now(),
                source=EpisodeType.text,
                group_id=group_id,
            )
        logger.info("storybuilding episode 入图完成：group=%s files=%d", group_id, len(files_to_ingest))

    # ------------------------------------------------------------------
    # 原语 4：健康检查（TTL 缓存）
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """FalkorDB 连通性检查（TTL 缓存 30 秒）。

        memory_recall middleware 每次 before_model 调一次。若不缓存，每章写作
        多一次网络往返。30 秒缓存：FalkorDB 挂是低频事件，窗口内过时可接受。
        """
        now = time.monotonic()
        if self._health_cache is not None:
            is_healthy, cached_at = self._health_cache
            if now - cached_at < _HEALTH_CACHE_TTL:
                return is_healthy

        # 实时检查：FalkorDB PING
        try:
            driver = self._client.driver
            await driver.execute_query("RETURN 1")
            self._health_cache = (True, now)
            return True
        except Exception as e:
            logger.warning("FalkorDB 健康检查失败：%s", e)
            self._health_cache = (False, now)
            return False

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _ensure_indices(self) -> None:
        """首次使用时建索引（Graphiti 要求）。幂等。"""
        if self._indices_ready:
            return
        await self._client.build_indices_and_constraints()
        self._indices_ready = True

    @property
    def client(self) -> "Graphiti":
        """暴露 Graphiti 客户端（供 ingestion 管道等需要直接操作的场景）。"""
        return self._client


__all__ = ["MemoryBackend", "EvidencePacket"]
