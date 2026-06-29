"""进化流水线文档落盘 / 读取（决策 D13 / D16 / E23）。

三文档统一格式：markdown + YAML front matter。
  - front matter：结构化字段（机器可解析，yaml.safe_load）。
  - 正文：自然语言诊断 / 详述（人可读，LLM 可消费）。

文档清单：
  eval_report.md     评估子代理产出（内容分 + 流程指标 + 流程诊断）
  design_doc.md      方案子代理产出（结构化改动列表 + 自然语言详述）
  change_log.md      执行子代理产出（落地了哪些改动 + validate 结果）

落盘位置：evolution/data/evolve_workspace/<session_id>/（每 session 独立目录）。

设计依据：设计文档 D13（markdown+YAML）/ D16（表存路径）/ E23（文档落盘传递）。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("evolution.evolve.docs")


# ── 文档根目录 ─────────────────────────────────────────────────


def _docs_root() -> Path:
    """evolve_workspace 目录（所有 session 文档的根）。"""
    return (
        Path(__file__).resolve().parent.parent.parent
        / "data"
        / "evolve_workspace"
    )


def session_dir(session_id: str) -> Path:
    """单个 session 的文档目录（不存在则创建）。"""
    d = _docs_root() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── markdown + YAML front matter 通用读写 ──────────────────────


def _dump_doc(path: Path, meta: dict[str, Any], body: str) -> str:
    """写一个 markdown + YAML front matter 文档。

    Args:
        path: 落盘路径
        meta: front matter 结构化字段（dict）
        body: markdown 正文

    Returns:
        相对路径（存进 DB 的 *_path 字段）
    """
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    content = f"---\n{front}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    logger.info("文档落盘: %s", path)
    return str(path)


def _load_doc(path: Path | str) -> tuple[dict[str, Any], str]:
    """读一个 markdown + YAML front matter 文档。

    Returns:
        (meta, body) —— meta 是 front matter dict，body 是 markdown 正文。
        无 front matter 时 meta={}，body 是全文。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text.strip()
    # 分割 front matter（首尾 --- 之间）
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2].strip()
    return meta, body


# ── eval_report.md（评估子代理产出）─────────────────────────────


def write_eval_report(
    session_id: str,
    *,
    trace_id: str,
    trace_kind: str,  # "baseline" | "candidate"
    content_scores: dict[str, Any],  # 内容层分数（复用 evaluation.py）
    flow_metrics: dict[str, Any],  # 流程硬指标（flow_metrics.py）
    findings: list[dict[str, str]],  # 流程诊断条目（见下方 schema）
    summary: str,  # 自然语言总述
) -> str:
    """评估子代理产出 eval_report.md。

    findings schema（每条）：
        {
          "dimension": "协作拓扑|错误保障|资源消耗|内容质量",
          "severity": "high|medium|low",
          "evidence_type": "实证|推断",      # E21：区分实证问题 vs 推断建议
          "finding": "问题描述",
          "evidence": "trace 证据（节点/指标）",
          "suggestion": "改进建议"
        }
    """
    path = session_dir(session_id) / f"eval_report_{trace_kind}.md"
    meta = {
        "trace_id": trace_id,
        "trace_kind": trace_kind,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "content_scores": content_scores,
        "flow_metrics": flow_metrics,
        "findings_count": len(findings),
        "findings": findings,
    }
    # 正文：自然语言总述 + 诊断条目展开
    lines = [f"# 评估报告（{trace_kind}）", "", summary, ""]
    if findings:
        lines.append("## 流程诊断条目")
        lines.append("")
        for i, f in enumerate(findings, 1):
            lines.append(f"### {i}. [{f.get('severity', '?').upper()}] {f.get('dimension', '?')}")
            lines.append(f"- **类型**：{f.get('evidence_type', '?')}")
            lines.append(f"- **发现**：{f.get('finding', '')}")
            lines.append(f"- **证据**：{f.get('evidence', '')}")
            lines.append(f"- **建议**：{f.get('suggestion', '')}")
            lines.append("")
    body = "\n".join(lines)
    return _dump_doc(path, meta, body)


def read_eval_report(path: str) -> dict[str, Any]:
    """读 eval_report.md，返回 {meta, body}。"""
    meta, body = _load_doc(path)
    return {"meta": meta, "body": body}


# ── design_doc.md（方案子代理产出）──────────────────────────────


def write_design_doc(
    session_id: str,
    *,
    changes: list[dict[str, Any]],  # 结构化改动列表（见下方 schema）
    rationale: str,  # 自然语言总述（基于 eval_report 的整体判断）
) -> str:
    """方案子代理产出 design_doc.md。

    changes schema（每条，D11/E16 结构化字段）：
        {
          "target": "文件/agent/section/key 或 源码路径",
          "change_desc": "改什么（描述性）",
          "reason": "依据评估证据",
          "expected_up": "预期涨的方面",
          "expected_down": "预期跌的方面（诚实声明）",
          "edit": {  # 可选：直接给 apply_edits 的指令（执行子代理消费）
              "op": "replace|insert|remove",
              "target": ["agent", "processors|slots", key],
              "spec": {"class": "...", "params": {...}}
          }
        }
    """
    path = session_dir(session_id) / "design_doc.md"
    meta = {
        "designed_at": datetime.now(UTC).isoformat(),
        "changes_count": len(changes),
        "changes": changes,
    }
    lines = ["# 改动设计文档", "", rationale, ""]
    if changes:
        lines.append("## 改动清单")
        lines.append("")
        for i, c in enumerate(changes, 1):
            lines.append(f"### {i}. {c.get('target', '?')}")
            lines.append(f"- **改什么**：{c.get('change_desc', '')}")
            lines.append(f"- **为什么**：{c.get('reason', '')}")
            lines.append(f"- **预期↑**：{c.get('expected_up', '')}")
            lines.append(f"- **预期↓**：{c.get('expected_down', '')}")
            if c.get("edit"):
                lines.append(f"- **edit 指令**：`{c['edit']}`")
            lines.append("")
    body = "\n".join(lines)
    return _dump_doc(path, meta, body)


def read_design_doc(path: str) -> dict[str, Any]:
    """读 design_doc.md，返回 {meta, body}。"""
    meta, body = _load_doc(path)
    return {"meta": meta, "body": body}


# ── change_log.md（执行子代理产出）──────────────────────────────


def write_change_log(
    session_id: str,
    *,
    applied: list[dict[str, Any]],  # 已落地的改动（含结果）
    validation: dict[str, Any],  # validate_changes 结果
    summary: str,  # 自然语言总述
) -> str:
    """执行子代理产出 change_log.md。

    applied schema（每条）：
        {
          "target": "改动目标",
          "action": "edit_file|write_file|apply_edits",
          "result": "ok|failed",
          "detail": "落地细节/错误"
        }
    validation schema：
        {"passed": bool, "config_valid": bool, "import_ok": bool, "errors": [str]}
    """
    path = session_dir(session_id) / "change_log.md"
    meta = {
        "executed_at": datetime.now(UTC).isoformat(),
        "applied_count": len(applied),
        "applied": applied,
        "validation": validation,
    }
    lines = ["# 执行改动记录", "", summary, ""]
    lines.append(f"## 校验结果：{'通过' if validation.get('passed') else '失败'}")
    lines.append("")
    if applied:
        lines.append("## 落地清单")
        lines.append("")
        for i, a in enumerate(applied, 1):
            mark = "✅" if a.get("result") == "ok" else "❌"
            lines.append(f"{i}. {mark} [{a.get('action', '?')}] {a.get('target', '?')}")
            if a.get("detail"):
                lines.append(f"   - {a['detail']}")
            lines.append("")
    body = "\n".join(lines)
    return _dump_doc(path, meta, body)


def read_change_log(path: str) -> dict[str, Any]:
    """读 change_log.md，返回 {meta, body}。"""
    meta, body = _load_doc(path)
    return {"meta": meta, "body": body}


__all__ = [
    "session_dir",
    "write_eval_report",
    "read_eval_report",
    "write_design_doc",
    "read_design_doc",
    "write_change_log",
    "read_change_log",
]
