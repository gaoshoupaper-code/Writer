"""评估输入提取器：按 subagent 从 trace 提取文件交付物。

职责（T1.1，决策 D12）：
  - 按 subagent 分组，取各 subagent 写到文件的最终内容（文件交付物），
    而非 llm_end 的 output（后者可能是中间思考过程）。

实现策略（经真实 trace 验证修正）：
  - 路径提取：从 tool_end 事件的 tool_output.content（"Updated file /xxx.md"）
    用正则提取文件路径。trace 里 write_file 只记录"已更新 XX 文件"，
    不含文件内容（recorder 的 _sanitize 清空了 tool_calls args）。
  - subagent 归属：靠事件的 agent_name 字段（interview-subagent 等），
    比 path pattern 更准（已验证）。
  - 内容读取：从 workspace 文件系统读（executor_workspace/{workspace_id}{path}）。
    文件内容不在 trace 里，只能从磁盘读。

各 subagent 交付物（按 agent_name 归属，已验证）：
  - interview-subagent      → /demand.md
  - storybuilding-subagent  → /character/*.md, /worldview.md, /storyline.md, /storyline/*.md
  - detail-outline-subagent → /detail/**
  - writing-subagent        → /chapter/**
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import app.core.db as db
from app.core.settings import settings

logger = logging.getLogger("evolution.eval_extractor")

# ── subagent → 交付物 path pattern 映射（兜底：agent_name 缺失时用）──
AGENT_DELIVERY_PATTERNS: dict[str, list[str]] = {
    "interview": ["/demand.md"],
    "storybuilding": ["/character/", "/worldview.md", "/storyline"],
    "detail-outline": ["/detail/"],
    "writing": ["/chapter/"],
}

# 评估关注的 4 个 primary subagent
EVALUATION_AGENTS = list(AGENT_DELIVERY_PATTERNS.keys())

# trace 里 agent_name 带 -subagent 后缀（如 interview-subagent）
# 归一化映射：trace agent_name → 短名
_AGENT_NAME_MAP = {
    "interview-subagent": "interview",
    "storybuilding-subagent": "storybuilding",
    "detail-outline-subagent": "detail-outline",
    "writing-subagent": "writing",
}

# 单文件交付物文本截断长度（控制 judge token 成本）
_DELIVERY_TRUNCATE = 6000

# 从 "Updated file /xxx.md" 提取路径的正则
_PATH_RE = re.compile(r"(?:Updated file|Wrote file)\s+(/[\w\-./]+\.\w+)", re.IGNORECASE)


def _normalize_agent_name(agent_name: str | None) -> str | None:
    """trace 的 agent_name（带 -subagent 后缀）→ 短名。"""
    if not agent_name:
        return None
    return _AGENT_NAME_MAP.get(agent_name)


def _path_belongs_to_agent(path: str, agent: str) -> bool:
    """兜底判断：path 是否属于某 agent（agent_name 缺失时用 path pattern）。"""
    patterns = AGENT_DELIVERY_PATTERNS.get(agent, [])
    return any(pat in path for pat in patterns)


def _resolve_workspace_path(workspace_id: str, file_path: str) -> Path:
    """解析文件在文件系统的绝对路径。

    workspace 根 = settings.executor_workspace_path（已解析为绝对路径，相对项目根）。
    子目录 = workspace_id。file_path 形如 /demand.md（前导 / 去除）。
    """
    rel = file_path.lstrip("/")
    return settings.executor_workspace_path / workspace_id / rel


def extract_deliveries(trace_id: str) -> dict[str, dict[str, str]]:
    """提取一个 trace 各 subagent 的文件交付物。

    流程：
      1. 从 event_payloads 取所有 tool_end（write_file）事件
      2. 从 "Updated file /xxx.md" 提取路径，按 agent_name 归属
      3. 从 workspace 文件系统读文件内容
      4. 同 path 多次写入只保留一次（文件已是最终态）

    Returns:
        {agent_short_name: {normalized_path: content, ...}, ...}
        只含实际有交付物（路径 + 可读内容）的 subagent。
    """
    # 取 trace 的 workspace_id（用于定位文件系统）
    run = db.query_one("SELECT workspace_id FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        logger.warning("extract_deliveries: trace 不存在 %s", trace_id)
        return {}
    workspace_id = run["workspace_id"]

    # 取所有 write_file 的 tool_end 事件
    rows = db.query_all(
        """SELECT payload_json FROM event_payloads
           WHERE trace_id = ? AND type = 'tool_end'
             AND payload_json LIKE '%write_file%'
           ORDER BY sequence ASC""",
        (trace_id,),
    )

    # agent → set(paths)（用 set 去重，文件系统读的是最终态）
    agent_paths: dict[str, set[str]] = {a: set() for a in EVALUATION_AGENTS}

    for row in rows:
        import json
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        tool_output = payload.get("tool_output")
        if not isinstance(tool_output, dict):
            continue
        content_text = tool_output.get("content", "")
        if not isinstance(content_text, str):
            continue

        # 提取路径
        match = _PATH_RE.search(content_text)
        if not match:
            continue
        file_path = match.group(1)

        # 归属 agent：优先 agent_name 字段，兜底 path pattern
        agent = _normalize_agent_name(payload.get("agent_name"))
        if agent is None:
            for candidate in EVALUATION_AGENTS:
                if _path_belongs_to_agent(file_path, candidate):
                    agent = candidate
                    break
        if agent is None:
            continue

        agent_paths[agent].add(file_path)

    # 从文件系统读内容
    result: dict[str, dict[str, str]] = {}
    for agent, paths in agent_paths.items():
        agent_files: dict[str, str] = {}
        for path in paths:
            abs_path = _resolve_workspace_path(workspace_id, path)
            try:
                text = abs_path.read_text(encoding="utf-8")
                if text.strip():
                    agent_files[path] = text[:_DELIVERY_TRUNCATE]
            except (OSError, UnicodeDecodeError):
                logger.debug("读取交付物失败 %s", abs_path)
        if agent_files:
            result[agent] = agent_files

    return result


def get_agent_delivery_text(trace_id: str, agent_name: str) -> str:
    """取某 subagent 的交付物拼接文本（供 subagent 维度 judge 用）。

    agent_name 用短名（interview/storybuilding/detail-outline/writing）。
    多文件按 path 排序拼接，每段加文件路径标注。无交付物返回空串。
    """
    deliveries = extract_deliveries(trace_id)
    agent_files = deliveries.get(agent_name, {})
    if not agent_files:
        return ""

    parts: list[str] = []
    for path in sorted(agent_files.keys()):
        parts.append(f"## 文件: {path}\n\n{agent_files[path]}")
    return "\n\n---\n\n".join(parts)


def get_content_layer_text(trace_id: str) -> str:
    """取内容维度 judge 用的文本（writing subagent 的全部正文交付物）。

    内容维度评的是"作品好不好看"，主要载体是 writing 的 chapter 正文。
    """
    return get_agent_delivery_text(trace_id, "writing")


def summarize_deliveries(trace_id: str) -> dict[str, Any]:
    """提取交付物的概要（不含正文，供页面/日志展示）。"""
    deliveries = extract_deliveries(trace_id)
    summary: dict[str, Any] = {}
    for agent, files in deliveries.items():
        summary[agent] = {
            "file_count": len(files),
            "total_chars": sum(len(c) for c in files.values()),
            "paths": sorted(files.keys()),
        }
    return summary
