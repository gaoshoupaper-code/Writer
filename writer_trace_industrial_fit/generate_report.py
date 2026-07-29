#!/usr/bin/env python3
"""Render the research JSON files as a reproducible Markdown evidence catalog."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


CATEGORY_MAPPING = {
    "基本信息": ["basic_info", "基本信息"],
    "基本定位": ["basic_positioning", "基本定位"],
    "技术特性": ["technical_features", "technical_characteristics", "技术特性"],
    "Writer 架构适配性": ["writer_architecture_fit", "Writer 架构适配性"],
    "运行与因果语义": ["runtime_causality", "运行与因果语义"],
    "数据与治理": ["data_governance", "数据与治理"],
    "分析与进化": ["analysis_evolution", "分析与进化"],
    "工业证据": ["industrial_evidence", "工业证据"],
    "性能指标": ["performance_metrics", "performance", "性能指标"],
    "里程碑意义": ["milestone_significance", "milestones", "里程碑意义"],
    "商业信息": ["business_info", "commercial_info", "商业信息"],
    "竞争与生态": ["competition_ecosystem", "competition", "竞争与生态"],
    "历史沿革": ["history", "历史沿革"],
    "市场定位": ["market_positioning", "market", "市场定位"],
}

ITEM_FILE_MAP = {
    "跨服务上下文与异步因果": "cross_service_causality",
    "DeepAgent 执行拓扑": "deepagent_topology",
    "Skill 显式激活语义": "skill_activation",
    "Middleware 有效干预语义": "middleware_intervention",
    "Artifact 与进化血缘": "artifact_lineage",
    "完整性与可靠摄入": "integrity_ingestion",
    "完整载荷治理": "payload_governance",
    "Outcome 评估与进化指标": "outcome_evolution_metrics",
    "专用运行血缘分析界面": "lineage_analysis_ui",
    "OTel GenAI 与 OTLP 边界适配": "otel_genai_otlp_boundary",
    "OpenInference ATIF 与 OpenLineage 交换适配": "openinference_atif_openlineage",
    "网关 APM 与外部观测平台边界": "gateway_apm_external_boundary",
    "成熟 Agent 工程横向案例": "mature_agent_engineering_cases",
}


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value.lower())
    return normalized.strip("-")


def find_field(data: dict[str, Any], name: str, category: str) -> Any:
    if name in data:
        return data[name]

    keys = CATEGORY_MAPPING.get(category, [category])
    for key in keys:
        nested = data.get(key)
        if isinstance(nested, dict) and name in nested:
            return nested[name]
    for value in data.values():
        if isinstance(value, dict) and name in value:
            return value[name]
    return None


def is_unknown(name: str, value: Any, uncertain: set[str]) -> bool:
    if name in uncertain or value is None or value == "":
        return True
    if isinstance(value, str) and "[不确定]" in value:
        return True
    return False


def render_value(value: Any, depth: int = 0) -> str:
    if isinstance(value, dict):
        pairs = [f"{key}: {render_value(item, depth + 1)}" for key, item in value.items()]
        return "; ".join(pairs)
    if isinstance(value, list):
        if not value:
            return "无"
        if all(isinstance(item, dict) for item in value):
            lines = [f"- {render_value(item, depth + 1)}" for item in value]
            return "<br>".join(lines)
        values = [render_value(item, depth + 1) for item in value]
        return "、".join(values) if len(values) <= 3 else "<br>".join(f"- {item}" for item in values)
    text = str(value).replace("\n", "<br>")
    return f"> {text}" if len(text) > 100 and depth == 0 else text


def field_title(name: str, definition: dict[str, Any]) -> str:
    description = definition.get("description", "")
    return f"{name}（{description}）" if description else name


def load_definitions(path: Path) -> tuple[str, list[tuple[str, list[dict[str, Any]]]], set[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    categories = [
        (item["category"], item.get("fields", []))
        for item in data.get("field_categories", [])
    ]
    required = {
        field["name"]
        for _, fields in categories
        for field in fields
    }
    return data.get("topic", "Writer Trace 观测机制"), categories, required


def source_lines(data: dict[str, Any]) -> list[str]:
    sources = data.get("official_sources")
    if not isinstance(sources, list):
        return []
    lines: list[str] = []
    for source in sources:
        if isinstance(source, dict):
            label = source.get("来源") or source.get("source") or "来源"
            address = source.get("地址") or source.get("url") or ""
            purpose = source.get("用途") or source.get("purpose") or ""
            lines.append(f"- **{label}**：{address}{'；' + purpose if purpose else ''}")
    return lines


def render_item(
    name: str,
    data: dict[str, Any],
    categories: list[tuple[str, list[dict[str, Any]]]],
    defined_fields: set[str],
) -> str:
    uncertain = set(data.get("uncertain", []))
    sections = [f"## {name}"]
    used: set[str] = set()

    for category, fields in categories:
        lines: list[str] = []
        for definition in fields:
            field_name = definition["name"]
            value = find_field(data, field_name, category)
            if is_unknown(field_name, value, uncertain):
                continue
            used.add(field_name)
            lines.append(f"- **{field_title(field_name, definition)}**：{render_value(value)}")
        if lines:
            sections.append(f"### {category}\n" + "\n".join(lines))

    extras = []
    nested_keys = {key for keys in CATEGORY_MAPPING.values() for key in keys}
    for key, value in data.items():
        if key in used or key in defined_fields or key in {"uncertain", "_source_file"}:
            continue
        if key in nested_keys or is_unknown(key, value, uncertain):
            continue
        extras.append(f"- **{key}**：{render_value(value)}")
    if extras:
        sections.append("### 其他信息\n" + "\n".join(extras))

    if uncertain:
        sections.append("### 不确定项\n" + "\n".join(f"- {item}" for item in sorted(uncertain)))

    return "\n\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evidence_catalog.md")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    outline = yaml.safe_load((root / "outline.yaml").read_text(encoding="utf-8"))
    topic, categories, defined_fields = load_definitions(root / "fields.yaml")
    output_dir = root / outline["execution"]["output_dir"].replace("./", "")

    result_by_name: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(output_dir.glob("*.json")):
        result_by_name[path.stem] = (path, json.loads(path.read_text(encoding="utf-8")))

    items = outline.get("items", [])
    index_rows = []
    detail_sections = []
    all_source_lines: list[str] = []
    for item in items:
        stem = ITEM_FILE_MAP.get(item["name"])
        if stem is None:
            stem = re.sub(r"[^a-z0-9]+", "_", item["name"].lower()).strip("_")
        match = result_by_name.get(stem)
        if not match:
            continue
        _, data = match
        display_name = data.get("mechanism_name") or item["name"]
        classification = data.get("writer_classification", "")
        decision = data.get("adoption_decision", "")
        index_rows.append(
            f"- [{display_name}](#{slug(display_name)})"
            f"{f'：{classification}' if classification else ''}"
            f"{f' / {decision}' if decision else ''}"
        )
        detail_sections.append(render_item(display_name, data, categories, defined_fields))
        all_source_lines.extend(source_lines(data))

    report = [
        f"# {topic} 调研证据目录",
        "",
        "本目录由 `generate_report.py` 从 13 份结构化研究结果生成；最终架构裁剪见同目录 `report.md`。",
        "",
        "## 目录",
        *index_rows,
        "",
        *detail_sections,
    ]
    if all_source_lines:
        report.extend(["", "## 信息来源汇总", *dict.fromkeys(all_source_lines)])

    output_path = root / args.output
    output_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
