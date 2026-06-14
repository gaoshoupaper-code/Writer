"""storyline_graph 服务测试：覆盖重构后的 build_storyline_graph_data / is_stale / generate。

样本：1 主线 S01 + 1 支线 S02 + 3 事件，其中 E001 是交汇（属 S01,S02）。
"""
from __future__ import annotations

import os
from pathlib import Path

from app.writer.expert_agent.services.storyline_graph import (
    build_storyline_graph_data,
    generate_storyline_graph,
    is_stale,
)


def _write_sample(workspace: Path) -> None:
    """写入最小 storyline 样本：一览表 + 两条线详情（含 1 交汇事件）。"""
    (workspace / "storyline.md").write_text(
        "# 故事核心\n\n## Logline\n示例\n\n"
        "# 故事线一览表\n\n"
        "| ID | 名称 | 类型 | 状态 |\n|----|------|------|------|\n"
        "| S01 | 主线 | 主线 | 活跃 |\n| S02 | 支线 | 支线 | 活跃 |\n",
        encoding="utf-8",
    )
    sdir = workspace / "storyline"
    sdir.mkdir()
    (sdir / "S01-主线.md").write_text(
        "### S01-主线 [主线]\n\n"
        "- 关键事件：E001, E002, E003\n"
        "- 全局走向：从起到终\n"
        "- 状态：活跃\n\n"
        "#### G01 [发展-冲突]\n\n"
        "1. ### E001-交汇事件\n"
        "   - 类型：冲突\n"
        "   - 事件组：G01\n"
        "   - 所属故事线：S01, S02\n\n"
        "2. ### E002-主线事件\n"
        "   - 类型：反转\n"
        "   - 事件组：G01\n"
        "   - 所属故事线：S01\n\n"
        "3. ### E003-主线收束\n"
        "   - 类型：胜利\n"
        "   - 事件组：G01\n"
        "   - 所属故事线：S01\n",
        encoding="utf-8",
    )
    (sdir / "S02-支线.md").write_text(
        "### S02-支线 [支线]\n\n"
        "- 关键事件：E001\n"
        "- 全局走向：支线发展\n"
        "- 状态：活跃\n",
        encoding="utf-8",
    )


def test_build_parses_storylines_events_and_intersection(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    data = build_storyline_graph_data(tmp_path)
    assert data is not None
    assert len(data.storylines) == 2
    assert len(data.events) == 3
    # 交汇事件 E001 同时属于两条线
    assert set(data.events["E001"].storylines) == {"S01", "S02"}
    # 拓扑序覆盖全部事件
    assert set(data.t_map) == {"E001", "E002", "E003"}
    # markdown 含 mermaid 代码块
    assert "```mermaid" in data.markdown


def test_is_stale_detects_missing_and_fresh(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    assert is_stale(tmp_path) is True  # 图不存在 → 过期
    generate_storyline_graph(tmp_path)
    assert (tmp_path / "storyline_graph.md").exists()
    assert is_stale(tmp_path) is False  # 刚生成 → 新鲜


def test_is_stale_detects_stale_after_source_update(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    generate_storyline_graph(tmp_path)
    assert is_stale(tmp_path) is False
    # 显式把源文件 mtime 推到图之后（touch 在同秒内可能不推进 mtime）
    future = (tmp_path / "storyline_graph.md").stat().st_mtime + 10
    os.utime(tmp_path / "storyline.md", (future, future))
    assert is_stale(tmp_path) is True


def test_build_returns_none_and_generate_safe_when_no_sources(tmp_path: Path) -> None:
    assert build_storyline_graph_data(tmp_path) is None
    generate_storyline_graph(tmp_path)  # 无产物：不抛、不写
    assert not (tmp_path / "storyline_graph.md").exists()
