"""导入后端现有 prompt .md 到 monitoring（Phase 4 Task 4.6）。

把后端分散的 prompt .md 文件导入为 monitoring 的 version 1 + production label。
一次性导入，幂等（已存在的 prompt 跳过）。

用法：python -m app.prompt_import
"""

from __future__ import annotations

from pathlib import Path

import app.prompts_repo as repo

# 后端 prompt 文件清单（来自 Phase 4 代码调查）。
# name = prompt 线名（loader 用此名拉取），path 相对 backend 根。
# path = None 表示不读 .md，从代码内置常量导入（如 judge rubric）。
BACKEND_PROMPTS: list[tuple[str, str | None]] = [
    ("meta_system", "app/domains/writing/meta/prompts/system.md"),
    ("writing_system", "app/domains/writing/expert_agent/prompts/writing_system.md"),
    ("writing_evaluation", "app/domains/writing/expert_agent/prompts/writing_evaluation.md"),
    ("storybuilding_system", "app/domains/writing/expert_agent/prompts/storybuilding_system.md"),
    ("storybuilding_evaluation", "app/domains/writing/expert_agent/prompts/storybuilding_evaluation.md"),
    ("detail_outline_system", "app/domains/writing/expert_agent/prompts/detail_outline_system.md"),
    ("detail_outline_evaluation", "app/domains/writing/expert_agent/prompts/detail_outline_evaluation.md"),
    ("interview_system", "app/domains/writing/expert_agent/prompts/interview_system.md"),
    ("demand_template", "app/domains/writing/expert_agent/prompts/demand_template.md"),
    # image domain
    ("image_system", "app/domains/image/prompts/system.md"),
    # judge rubric：从代码内置常量导入（非后端 .md），配置化后可在 /prompts 页改。
    ("judge_rubric", None),
]


def import_backend_prompts(backend_root: Path) -> dict[str, int]:
    """导入后端 prompt 文件。幂等：已存在的 name 跳过。

    rel_path 为 None 的条目不从文件读，而是从代码内置常量取内容
    （如 judge_rubric）。

    Returns: {"imported": N, "skipped": M}
    """
    imported = 0
    skipped = 0
    for name, rel_path in BACKEND_PROMPTS:
        # 已存在则跳过（幂等）
        if repo.get_prompt_by_name(name) is not None:
            skipped += 1
            continue
        if rel_path is None:
            # 内置常量导入（judge rubric）
            from app.rubric import DEFAULT_RUBRIC, OUTPUT_FORMAT

            content = (DEFAULT_RUBRIC + "\n" + OUTPUT_FORMAT).strip()
            commit_message = "从代码内置 rubric 初始导入"
        else:
            full_path = backend_root / rel_path
            if not full_path.exists():
                print(f"  跳过（文件不存在）: {name} -> {rel_path}")
                skipped += 1
                continue
            content = full_path.read_text(encoding="utf-8").strip()
            commit_message = "从后端 .md 文件初始导入"
        prompt = repo.create_prompt(name, "text")
        repo.create_version(
            prompt["id"],
            content=content,
            commit_message=commit_message,
            source="manual",
            labels=["production", "latest"],
        )
        print(f"  导入: {name} (version 1, production)")
        imported += 1
    return {"imported": imported, "skipped": skipped}


if __name__ == "__main__":
    # backend 根 = monitoring 的上一级的 backend
    monitoring_root = Path(__file__).resolve().parent.parent
    project_root = monitoring_root.parent
    backend_root = project_root / "backend"
    if not backend_root.exists():
        print(f"后端目录不存在: {backend_root}")
        raise SystemExit(1)
    print(f"从 {backend_root} 导入 prompt...")
    result = import_backend_prompts(backend_root)
    print(f"完成：导入 {result['imported']}，跳过 {result['skipped']}")
