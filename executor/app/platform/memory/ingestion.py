"""异步入图管道——管理 storybuilding + chapter 两个入图触发点。

被 WritingEventSink 的 _on_storybuilding_done / _on_writing_chapter_done 调用。
用 asyncio.to_thread 包装（与 generate_storyline_graph 同构），不阻塞 SSE 流。

失败语义（设计文档 §1.4 + §3.6）：
  入图是异步的，SSE 流不等它。失败时写 workspace 级 .memory_unhealthy flag，
  下一次 memory_recall middleware 检测到 flag 则报错中断写作。

为什么入图管道单独成模块而非内联在 events.py：
  入图涉及"从 harness 包加载解析器 + 调 MemoryBackend + 失败标记"多个步骤，
  逻辑比 generate_storyline_graph（纯后端函数）复杂。内联会让 events.py 膨胀，
  且入图逻辑与 SSE 事件分发是不同关注点。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from app.platform.memory.client import get_memory_backend

logger = logging.getLogger(__name__)

_UNHEALTHY_FLAG = ".memory_unhealthy"


def _group_id(owner_id: str | None, workspace_path: Path) -> str:
    """构造 group_id（owner_id + workspace 名）。"""
    ws_name = workspace_path.name
    return f"{owner_id}:{ws_name}" if owner_id else ws_name


def _mark_unhealthy(workspace_path: Path, reason: str) -> None:
    """入图失败后写 workspace 级 flag，阻断后续 memory_recall。"""
    flag = workspace_path / _UNHEALTHY_FLAG
    flag.write_text(
        f"{datetime.now().isoformat()}\n{reason}\n", encoding="utf-8"
    )
    logger.warning("记忆系统标记不健康：workspace=%s reason=%s", workspace_path.name, reason)


def clear_unhealthy(workspace_path: Path) -> None:
    """入图成功后清除 flag（恢复健康）。"""
    flag = workspace_path / _UNHEALTHY_FLAG
    if flag.exists():
        flag.unlink()


def is_unhealthy(workspace_path: Path) -> bool:
    """检查 workspace 是否被标记为记忆不健康（memory_recall middleware 调用）。"""
    return (workspace_path / _UNHEALTHY_FLAG).exists()


def _load_harness_parsers() -> tuple[object | None, object | None, object | None]:
    """从 harness 包加载可进化的解析器（storyline_parser/story_calendar/character_parser）。

    harness 包是 git 独立仓库，运行时动态加载为 harness_current 模块。
    解析器尚未实现时返回 None（降级到 episode 入图）。

    返回 (storyline_parser, story_calendar, character_parser)。
    """
    try:
        from app.platform.agent.loader import load_current_package

        pkg = load_current_package()
        storyline_parser = getattr(pkg, "storyline_parser", None)
        story_calendar = getattr(pkg, "story_calendar", None)
        character_parser = getattr(pkg, "character_parser", None)
        return storyline_parser, story_calendar, character_parser
    except Exception as e:
        logger.debug("harness 解析器加载失败（降级到 episode 入图）：%s", e)
        return None, None, None


# ── storybuilding 入图 ──────────────────────────────────────────────

async def ingest_storybuilding(
    workspace_path: Path,
    owner_id: str | None,
) -> None:
    """storybuilding 完成后，把蓝图产物结构化入图。

    被 WritingEventSink._on_storybuilding_done 调用。
    """
    backend = get_memory_backend()
    if backend is None:
        logger.info("MemoryBackend 未启用，跳过 storybuilding 入图")
        return

    group_id = _group_id(owner_id, workspace_path)
    sp, cal, cp = _load_harness_parsers()

    try:
        await backend.ingest_storybuilding(
            workspace_path=workspace_path,
            group_id=group_id,
            storyline_parser=sp,
            story_calendar=cal,
            character_parser=cp,
        )
        clear_unhealthy(workspace_path)
        logger.info("storybuilding 入图成功：workspace=%s", workspace_path.name)
    except Exception as e:
        _mark_unhealthy(workspace_path, f"storybuilding 入图失败：{e}")
        logger.error("storybuilding 入图失败：workspace=%s error=%s", workspace_path.name, e, exc_info=True)


def ingest_storybuilding_sync(workspace_path: Path, owner_id: str | None) -> None:
    """同步包装（供 asyncio.to_thread 调用）。

    在线程里跑 asyncio 事件循环执行异步入图。
    """
    asyncio.run(ingest_storybuilding(workspace_path, owner_id))


# ── chapter 入图（Causal Publish Flow）──────────────────────────────

async def ingest_chapter(
    workspace_path: Path,
    owner_id: str | None,
    chapter_index: int,
) -> None:
    """章节写完后，把正文作为 episode 入图（Causal Publish Flow）。

    被 WritingEventSink._on_writing_chapter_done 调用。
    """
    backend = get_memory_backend()
    if backend is None:
        logger.info("MemoryBackend 未启用，跳过 chapter 入图")
        return

    # 读章节正文
    chapter_path = workspace_path / "chapter" / f"chapter-{chapter_index:02d}.md"
    if not chapter_path.exists():
        # 备选命名（无前导零）
        chapter_path = workspace_path / "chapter" / f"chapter-{chapter_index}.md"
    if not chapter_path.exists():
        logger.warning("章节文件不存在，跳过入图：%s", chapter_path)
        return

    content = chapter_path.read_text(encoding="utf-8")
    if not content.strip():
        logger.warning("章节正文为空，跳过入图：chapter-%d", chapter_index)
        return

    group_id = _group_id(owner_id, workspace_path)

    # 加载 schema（harness 可进化）
    entity_types, edge_types = _load_harness_schema()

    try:
        await backend.add_episode(
            name=f"chapter-{chapter_index}",
            episode_body=content,
            reference_time=datetime.now(),
            group_id=group_id,
            entity_types=entity_types,
            edge_types=edge_types,
        )
        clear_unhealthy(workspace_path)
        logger.info("chapter 入图成功：chapter-%d workspace=%s", chapter_index, workspace_path.name)
    except Exception as e:
        _mark_unhealthy(workspace_path, f"chapter-{chapter_index} 入图失败：{e}")
        logger.error("chapter 入图失败：chapter-%d error=%s", chapter_index, e, exc_info=True)


def ingest_chapter_sync(workspace_path: Path, owner_id: str | None, chapter_index: int) -> None:
    """同步包装（供 asyncio.to_thread 调用）。"""
    asyncio.run(ingest_chapter(workspace_path, owner_id, chapter_index))


def _load_harness_schema() -> tuple[dict | None, dict | None]:
    """从 harness 包加载 narrative_schema（entity_types/edge_types）。

    解析器尚未实现时返回 (None, None)（降级到 Graphiti 默认抽取）。
    """
    try:
        from app.platform.agent.loader import load_current_package

        pkg = load_current_package()
        schema = getattr(pkg, "narrative_schema", None)
        if schema is None:
            # 尝试从 tools 子模块加载
            tools = getattr(pkg, "tools", None)
            if tools is not None:
                schema = getattr(tools, "narrative_schema", None)
        if schema is None:
            return None, None
        entity_types = getattr(schema, "ENTITY_TYPES", None)
        edge_types = getattr(schema, "EDGE_TYPES", None)
        return entity_types, edge_types
    except Exception as e:
        logger.debug("harness schema 加载失败（降级到默认抽取）：%s", e)
        return None, None


__all__ = [
    "ingest_storybuilding",
    "ingest_storybuilding_sync",
    "ingest_chapter",
    "ingest_chapter_sync",
    "is_unhealthy",
    "clear_unhealthy",
]
