"""NWM 记忆抽取管道——extract_and_publish：章节正文 → typed records → store。

被 WritingEventSink._on_writing_chapter_done 调用（events.py）。
用 asyncio.to_thread 包装，不阻塞 SSE 流。

数据流：
  读章节正文 → extractor.extract() → ChapterRecords
  → 为每条 record 拼 search_text（语义拼接，harness 可进化）
  → batch embed（智谱 embedding-3）
  → store.store_record() 写入（含 PlotPromise 状态机）

失败语义（D-R5-1）：
  抽取/embed/store 任一失败 → 写 .memory_unhealthy flag，
  下一次 memory_recall middleware 检测到 flag 则降级全量注入（不中断写作）。

设计依据：设计文档 D-D2-1（ingestion 管道职责）、D-D3-1（registry 追加）、D-D3-2（PlotPromise 状态机）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.platform.memory.embedder import get_memory_embedder
from app.platform.memory.extractor import (
    ChapterRecords,
    CharacterStateRecord,
    MemoryExtractError,
    NarrativeFunctionRecord,
    ObjectStateRecord,
    PlotPromiseRecord,
    RelationshipStateRecord,
    SceneRecord,
    WorldFactRecord,
    get_memory_extractor,
)
from app.platform.memory.store import MemoryStore, get_memory_store_pool

logger = logging.getLogger(__name__)

_UNHEALTHY_FLAG = ".memory_unhealthy"


# ── workspace 健康标记（跨组件阻断信号）──────────────────────────────

def _mark_unhealthy(workspace_path: Path, reason: str) -> None:
    """抽取失败后写 workspace 级 flag，阻断后续 memory_recall（降级全量注入）。"""
    flag = workspace_path / _UNHEALTHY_FLAG
    flag.write_text(f"{datetime.now().isoformat()}\n{reason}\n", encoding="utf-8")
    logger.warning("记忆系统标记不健康：workspace=%s reason=%s", workspace_path.name, reason)


def clear_unhealthy(workspace_path: Path) -> None:
    """抽取成功后清除 flag（恢复健康）。"""
    flag = workspace_path / _UNHEALTHY_FLAG
    if flag.exists():
        flag.unlink()


def is_unhealthy(workspace_path: Path) -> bool:
    """检查 workspace 是否被标记记忆不健康。"""
    return (workspace_path / _UNHEALTHY_FLAG).exists()


# ── harness 可进化要素加载 ────────────────────────────────────────────

# extractor prompt 缓存（harness 不变则只加载一次；reload_current 后进程重启重新加载）
_harness_extract_prompt: str | None = None
_harness_prompt_loaded = False


def _load_harness_extract_prompt() -> str | None:
    """加载 harness 的 memory_extraction_guide.md 作为 extractor system prompt。

    harness 是 git 独立仓库，executor 通过 load_current_package 定位。
    文件不存在或加载失败返回 None（extractor 用内置默认 prompt）。
    结果缓存到进程级（harness ship 后 reload_current 会重启进程，自然刷新）。
    """
    global _harness_extract_prompt, _harness_prompt_loaded
    if _harness_prompt_loaded:
        return _harness_extract_prompt
    _harness_prompt_loaded = True
    try:
        from app.platform.agent.loader import load_current_package
        from pathlib import Path as _P
        pkg = load_current_package()
        # harness 包路径：pkg.__file__ 是 repo/__init__.py
        pkg_dir = _P(pkg.__file__).resolve().parent
        prompt_path = pkg_dir / "prompts" / "memory_extraction_guide.md"
        if prompt_path.exists():
            _harness_extract_prompt = prompt_path.read_text(encoding="utf-8")
            logger.info("加载 harness extractor prompt：%s", prompt_path.name)
    except Exception as e:
        logger.debug("harness extractor prompt 加载失败，用 executor 默认：%s", e)
    return _harness_extract_prompt


def reset_harness_prompt_cache() -> None:
    """重置 prompt 缓存（测试用）。"""
    global _harness_extract_prompt, _harness_prompt_loaded
    _harness_extract_prompt = None
    _harness_prompt_loaded = False


# ── search_text 语义拼接（D-D1-3，默认版；harness 可覆盖）─────────────
# 把 record 的关键语义字段拼成自然语言文本，供 embed + FTS5 检索。
# 拼法影响检索质量，是 harness 可进化要素（Phase 5）。

def _join_list(items: list[str] | None) -> str:
    return "、".join(items) if items else ""


def build_search_text(record_type: str, record: Any) -> str:
    """默认 search_text 拼接器。harness 可传自定义 builder 覆盖。"""
    if record_type == "chapter_digest":
        r: ChapterRecords = record
        return f"第{getattr(record, 'source_chapter', '')}章摘要：{record.summary}。关键事件：{_join_list(record.key_events)}"
    if record_type == "scene":
        r: SceneRecord = record
        return f"场景{r.scene_id}：地点{r.location}，参与{_join_list(r.participants)}。{r.summary}"
    if record_type == "character_state":
        r: CharacterStateRecord = record
        parts = [f"角色{r.name}"]
        if r.goal: parts.append(f"目标{r.goal}")
        if r.knowledge: parts.append(f"知道{_join_list(r.knowledge)}")
        if r.unknowns: parts.append(f"不知道{_join_list(r.unknowns)}")
        if r.status: parts.append(f"状态{r.status}")
        if r.location: parts.append(f"位置{r.location}")
        if r.relationship_deltas: parts.append(f"关系变化{_join_list(r.relationship_deltas)}")
        return "，".join(parts)
    if record_type == "relationship_state":
        r: RelationshipStateRecord = record
        return f"关系：{r.char_a}与{r.char_b}，{r.relation_type}，{r.polarity}。{r.relationship_desc}"
    if record_type == "object_state":
        r: ObjectStateRecord = record
        return f"物品{r.name}：持有{r.owner}，位置{r.location}，状态{r.condition}"
    if record_type == "plot_promise":
        r: PlotPromiseRecord = record
        return f"伏笔{r.promise_id}：{r.structural_role}，状态{r.status}。{r.promised_payoff}{r.resolution}"
    if record_type == "narrative_function":
        r: NarrativeFunctionRecord = record
        parts = [f"叙事功能"]
        if r.focalized_observer: parts.append(f"视角{r.focalized_observer}")
        if r.dramatic_beat: parts.append(f"拍子{r.dramatic_beat}")
        if r.turn_or_reversal: parts.append(f"转折{r.turn_or_reversal}")
        if r.reader_knowledge: parts.append(f"读者知晓{r.reader_knowledge}")
        return "，".join(parts) + f"。{r.summary}"
    if record_type == "world_fact":
        r: WorldFactRecord = record
        return f"世界设定：{r.fact}（{r.category}）。范围{r.scope}"
    return str(record)


# ── extract_and_publish 主流程 ────────────────────────────────────────

async def extract_and_publish(
    workspace_path: Path,
    workspace_id: str,
    chapter_index: int,
    *,
    search_text_builder: Callable[[str, Any], str] | None = None,
    record_types_enabled: list[str] | None = None,
    publish_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """章节正文 → typed records → store（Causal Publish Flow）。

    Args:
        workspace_path: workspace 根目录（读 chapter/chapter-XX.md + 写 unhealthy flag）。
        workspace_id: 作品隔离标识（定位 memory.db）。
        chapter_index: 章节号（因果锚点）。
        search_text_builder: 自定义 search_text 拼接器（harness 可进化，None 用默认）。
        record_types_enabled: 启用的 record 类型（harness record_type_policy，None 全启用）。
        publish_callback: 抽取统计回调（写 trace run_meta 用，Phase 4 接入）。
            接收 dict：{chapter_index, stats, duration_ms, ok, error}。None 不埋点。
        workspace_path: workspace 根目录（读 chapter/chapter-XX.md + 写 unhealthy flag）。
        workspace_id: 作品隔离标识（定位 memory.db）。
        chapter_index: 章节号（因果锚点）。
        search_text_builder: 自定义 search_text 拼接器（harness 可进化，None 用默认）。
        record_types_enabled: 启用的 record 类型（harness record_type_policy，None 全启用）。

    Returns:
        统计 dict：各 record 类型写入条数（供 trace 埋点）。

    Raises:
        MemoryExtractError: 抽取失败（调用方据此标 unhealthy）。
    """
    builder = search_text_builder or build_search_text
    start_ts = time.monotonic()

    # 1. 读章节正文
    chapter_path = workspace_path / "chapter" / f"chapter-{chapter_index:02d}.md"
    if not chapter_path.exists():
        chapter_path = workspace_path / "chapter" / f"chapter-{chapter_index}.md"
    if not chapter_path.exists():
        logger.warning("章节文件不存在，跳过抽取：chapter-%d", chapter_index)
        return {}
    chapter_text = chapter_path.read_text(encoding="utf-8")
    if not chapter_text.strip():
        logger.warning("章节正文为空，跳过抽取：chapter-%d", chapter_index)
        return {}

    # 2. 抽取 typed records
    extractor = get_memory_extractor()
    if extractor is None:
        logger.info("抽取器未启用，跳过 chapter-%d 抽取", chapter_index)
        return {}
    # harness 可进化 extractor prompt（memory_extraction_guide.md）；None 用 executor 默认
    system_prompt = _load_harness_extract_prompt()
    records = await extractor.extract(chapter_text, chapter_index, system_prompt=system_prompt)

    # 3. 构造待写入条目列表（record_type, entity_name, fields, search_text, evidence_span）
    #    并批量 embed 所有 search_text（一次 API 调用）
    entries = _collect_entries(records, builder, record_types_enabled)

    # 4. batch embed（embedder 可能 None：向量检索降级到纯 BM25/LIKE）
    search_texts = [e["search_text"] for e in entries]
    embeddings: list[list[float] | None]
    embedder = get_memory_embedder()
    if embedder is not None and search_texts:
        try:
            vecs = await embedder.embed(search_texts)
            embeddings = list(vecs) + [None] * (len(search_texts) - len(vecs))
        except Exception as e:
            logger.warning("embed 失败，本批记录跳过向量（BM25/LIKE 仍可用）：%s", e)
            embeddings = [None] * len(search_texts)
    else:
        embeddings = [None] * len(search_texts)

    # 5. 写入 store（含 PlotPromise 状态机）
    pool = get_memory_store_pool()
    store = await pool.get(workspace_id)

    stats: dict[str, int] = {}
    for entry, emb in zip(entries, embeddings):
        _write_entry(store, entry, emb, chapter_index, stats)

    clear_unhealthy(workspace_path)
    duration_ms = int((time.monotonic() - start_ts) * 1000)
    logger.info(
        "chapter-%d 抽取入库完成：workspace=%s stats=%s duration=%dms",
        chapter_index, workspace_id, stats, duration_ms,
    )
    # 抽取质量埋点（Phase 4 由 events.py 接入 publish_callback 写 trace run_meta）
    if publish_callback is not None:
        try:
            publish_callback({
                "chapter_index": chapter_index,
                "stats": stats,
                "total_records": sum(stats.values()),
                "duration_ms": duration_ms,
                "ok": True,
                "error": None,
            })
        except Exception:
            pass  # 埋点失败不影响主流程
    return stats


def _collect_entries(
    records: ChapterRecords,
    builder: Callable[[str, Any], str],
    enabled: list[str] | None,
) -> list[dict[str, Any]]:
    """把 ChapterRecords 展平成待写入条目列表。

    每个条目：{record_type, entity_name, fields, search_text, evidence_span}
    enabled 非 None 时跳过禁用类型（题材驱动，D-D3-3）。
    """
    def enabled_for(rt: str) -> bool:
        return enabled is None or rt in enabled

    entries: list[dict[str, Any]] = []

    # chapter_digest（每章一条，实体名=章节号字符串）
    if enabled_for("chapter_digest"):
        cd = records.chapter_digest
        entries.append({
            "record_type": "chapter_digest",
            "entity_name": "",  # chapter_digest 实体列复用 source_chapter，entity_name 不用
            "fields": {"summary": cd.summary, "key_events": cd.key_events},
            "search_text": builder("chapter_digest", cd),
            "evidence_span": cd.summary,  # 摘要本身即溯源
        })

    # scenes
    if enabled_for("scene"):
        for s in records.scenes:
            entries.append({
                "record_type": "scene", "entity_name": s.scene_id,
                "fields": {
                    "location": s.location, "participants": s.participants,
                    "event_order": s.event_order, "reveal_order": s.reveal_order,
                    "summary": s.summary,
                },
                "search_text": builder("scene", s), "evidence_span": s.evidence_span,
            })

    # characters
    if enabled_for("character_state"):
        for c in records.characters:
            entries.append({
                "record_type": "character_state", "entity_name": c.name,
                "fields": {
                    "goal": c.goal, "knowledge": c.knowledge, "unknowns": c.unknowns,
                    "status": c.status, "location": c.location,
                    "relationship_deltas": c.relationship_deltas,
                },
                "search_text": builder("character_state", c), "evidence_span": c.evidence_span,
            })

    # relationships
    if enabled_for("relationship_state"):
        for r in records.relationships:
            entries.append({
                "record_type": "relationship_state", "entity_name": r.char_a,
                "fields": {
                    "char_b": r.char_b, "relation_type": r.relation_type,
                    "polarity": r.polarity, "relationship_desc": r.relationship_desc,
                },
                "search_text": builder("relationship_state", r), "evidence_span": r.evidence_span,
            })

    # objects
    if enabled_for("object_state"):
        for o in records.objects:
            entries.append({
                "record_type": "object_state", "entity_name": o.name,
                "fields": {"owner": o.owner, "location": o.location, "condition": o.condition},
                "search_text": builder("object_state", o), "evidence_span": o.evidence_span,
            })

    # promises（状态机：open 新建，closed/updated 更新现有）
    if enabled_for("plot_promise"):
        for p in records.promises:
            entries.append({
                "record_type": "plot_promise", "entity_name": p.promise_id,
                "fields": {
                    "thread_id": p.thread_id, "structural_role": p.structural_role,
                    "status": p.status, "setup_chapter": p.setup_chapter_hint or 0,
                    "promised_payoff": p.promised_payoff, "resolution": p.resolution,
                },
                "search_text": builder("plot_promise", p), "evidence_span": p.evidence_span,
                # promise 特殊标记：交给 _write_entry 判断 INSERT vs UPDATE
                "_promise_status": p.status,
                "_promise_setup_hint": p.setup_chapter_hint,
            })

    # narrative_functions
    if enabled_for("narrative_function"):
        for n in records.narrative_functions:
            entries.append({
                "record_type": "narrative_function", "entity_name": n.scene_ref,
                "fields": {
                    "focalized_observer": n.focalized_observer,
                    "dramatic_beat": n.dramatic_beat, "turn_or_reversal": n.turn_or_reversal,
                    "reader_knowledge": n.reader_knowledge, "summary": n.summary,
                },
                "search_text": builder("narrative_function", n), "evidence_span": n.evidence_span,
            })

    # world_facts
    if enabled_for("world_fact"):
        for w in records.world_facts:
            entries.append({
                "record_type": "world_fact", "entity_name": w.fact,
                "fields": {"category": w.category, "scope": w.scope},
                "search_text": builder("world_fact", w), "evidence_span": w.evidence_span,
            })

    return entries


def _write_entry(
    store: MemoryStore,
    entry: dict[str, Any],
    embedding: list[float] | None,
    chapter_index: int,
    stats: dict[str, int],
) -> None:
    """写一条 record（PlotPromise 走状态机分支）。

    论文 §3.2：promise 的 open/closed 是 first-class 状态。
    - status=open 且 setup_chapter_hint>0：本章新铺设 → INSERT 新记录。
    - status=closed/updated：兑现/推进旧伏笔 → UPDATE 已有记录状态。
    """
    rt = entry["record_type"]

    # PlotPromise 状态机分支
    if rt == "plot_promise":
        status = entry.get("_promise_status", "open")
        setup_hint = entry.get("_promise_setup_hint", 0)
        promise_id = entry["entity_name"]
        fields = entry["fields"]

        if status == "closed":
            # 兑现：UPDATE 已有 open 记录
            updated = store.update_plot_promise_status(
                promise_id, status="closed",
                payoff_chapter=chapter_index,
                resolution=fields.get("resolution", ""),
            )
            if updated:
                stats[rt] = stats.get(rt, 0) + 1
                return
            # 没匹配到 open 记录：可能是顺序异常（兑现先于铺设），降级为 INSERT
            logger.warning("promise %s 标记 closed 但无 open 记录，降级为新建", promise_id)

        # open 新铺设 或 异常降级：INSERT
        store.store_record(
            rt, promise_id, fields,
            source_chapter=chapter_index,
            evidence_span=entry["evidence_span"],
            search_text=entry["search_text"],
            embedding=embedding,
        )
        stats[rt] = stats.get(rt, 0) + 1
        return

    # 普通 record：直接 INSERT（追加，registry 取最新）
    store.store_record(
        rt, entry["entity_name"], entry["fields"],
        source_chapter=chapter_index,
        evidence_span=entry["evidence_span"],
        search_text=entry["search_text"],
        embedding=embedding,
    )
    stats[rt] = stats.get(rt, 0) + 1


# ── 同步包装（供 events.py 的 asyncio.to_thread 调用）─────────────────

def extract_and_publish_sync(
    workspace_path: Path,
    workspace_id: str,
    chapter_index: int,
) -> dict[str, int]:
    """同步包装：在线程里跑 asyncio 事件循环执行抽取入库。

    被 events.py 的 _on_writing_chapter_done 通过 asyncio.to_thread 调用。
    失败时写 .memory_unhealthy flag（D-R5-1 降级语义）。
    """
    try:
        return asyncio.run(extract_and_publish(workspace_path, workspace_id, chapter_index))
    except MemoryExtractError as e:
        _mark_unhealthy(workspace_path, f"chapter-{chapter_index} 抽取失败：{e}")
        logger.error("chapter-%d 抽取失败：%s", chapter_index, e, exc_info=True)
        return {}
    except Exception as e:
        _mark_unhealthy(workspace_path, f"chapter-{chapter_index} 入库失败：{e}")
        logger.error("chapter-%d 入库失败：%s", chapter_index, e, exc_info=True)
        return {}


__all__ = [
    "extract_and_publish",
    "extract_and_publish_sync",
    "build_search_text",
    "is_unhealthy",
    "clear_unhealthy",
]
