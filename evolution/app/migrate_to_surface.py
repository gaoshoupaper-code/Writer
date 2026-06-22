"""v1 harness → surface 体系迁移脚本（Phase 6 T2.1/T2.3，决策 D14 一次性切换）。

把 v1 的 harness 数据（prompt .md / SKILL.md / description / middleware 参数 / permissions /
带 state_schema 的 middleware 代码）导入 surface_versions 表，并生成初始 production manifest。

幂等：重复跑会先清空 surface_versions/harness_manifests 再重新导入（迁移脚本是
「重建基准」语义，不是增量）。生产环境首次切换后不应再跑。

用法：
    cd evolution
    python -m app.migrate_to_surface              # 默认导入 + 发布 manifest
    python -m app.migrate_to_surface --dry-run     # 只打印不写库
    python -m app.migrate_to_surface --import-only # 只导 surface，不发布 manifest

数据源（已核实，见 Phase 2 调研清单）：
  - A 类 prompt: executor/app/domains/writing/{expert_agent,meta}/prompts/*.md
  - A 类 skill:  executor/app/domains/writing/**/skills/**/SKILL.md
  - A 类 description: v1 subagents.py 的 build_description 字面量（本脚本内联）
  - B 类 middleware_params: v1 build_middleware 实例化参数（本脚本内联）
  - B 类 permissions: v1 build_permissions 规则（本脚本内联）
  - C 类 stateful_middleware: goal_middleware.py 全文（state_schema=GoalState）

设计依据：设计文档 D13（提取即冻结）+ D14（独立迁移脚本）+ 迁移方案 Step 1-9。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.improvement import surface_repo, manifest_repo

logger = logging.getLogger("evolution.migrate_to_surface")

# 项目根 Writer/
# 注：settings._project_root 在当前 settings 实现里解析到 evolution/（非 Writer/），
# 迁移脚本要跨服务读 executor 目录，直接从本文件位置算 Writer/ 根。
# migrate_to_surface.py 在 evolution/app/，往上 1 级 = app/，2 级 = evolution/，3 级 = Writer/。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EXECUTOR_APP = _PROJECT_ROOT / "executor" / "app"
_PROMPTS_DIR = _EXECUTOR_APP / "domains" / "writing" / "expert_agent" / "prompts"
_META_PROMPTS_DIR = _EXECUTOR_APP / "domains" / "writing" / "meta" / "prompts"
_SKILLS_BASE = _EXECUTOR_APP / "domains" / "writing" / "expert_agent" / "skills"
_META_SKILLS_BASE = _EXECUTOR_APP / "domains" / "writing" / "meta" / "skills"
_GOAL_MW_PATH = _EXECUTOR_APP / "domains" / "writing" / "middleware" / "goal_middleware.py"

# A 类 prompt 映射：(name, scope, role, 文件路径)
# 与 _AGENT_PROMPT_SEED 对齐 + 补 meta_system
_PROMPT_SOURCES: list[tuple[str, str, str, Path]] = [
    ("interview_system", "interview", "primary", _PROMPTS_DIR / "interview_system.md"),
    ("storybuilding_system", "storybuilding", "primary", _PROMPTS_DIR / "storybuilding_system.md"),
    ("storybuilding_evaluation", "storybuilding", "evaluation", _PROMPTS_DIR / "storybuilding_evaluation.md"),
    ("detail_outline_system", "detail-outline", "primary", _PROMPTS_DIR / "detail_outline_system.md"),
    ("detail_outline_evaluation", "detail-outline", "evaluation", _PROMPTS_DIR / "detail_outline_evaluation.md"),
    ("writing_system", "writing", "primary", _PROMPTS_DIR / "writing_system.md"),
    ("writing_evaluation", "writing", "evaluation", _PROMPTS_DIR / "writing_evaluation.md"),
    ("meta_system", "meta", "primary", _META_PROMPTS_DIR / "system.md"),
]

# A 类 skill 映射：(name, scope, SKILL.md 路径, 相对 rel_dir)
# 用 frontmatter name 作 surface_name；rel_dir 相对 executor/app 供 loader 定位
_SKILL_SOURCES: list[tuple[str, str, Path, str]] = [
    ("storybuilding-initial", "storybuilding",
     _SKILLS_BASE / "storybuilding-initial" / "SKILL.md",
     "domains/writing/expert_agent/skills/storybuilding-initial"),
    ("storybuilding-expand", "storybuilding",
     _SKILLS_BASE / "storybuilding-expand" / "SKILL.md",
     "domains/writing/expert_agent/skills/storybuilding-expand"),
    ("detail-planning", "detail-outline",
     _SKILLS_BASE / "detail_outline" / "detail-planning" / "SKILL.md",
     "domains/writing/expert_agent/skills/detail_outline/detail-planning"),
    ("chapter-writing", "writing",
     _SKILLS_BASE / "writing" / "chapter-writing" / "SKILL.md",
     "domains/writing/expert_agent/skills/writing/chapter-writing"),
    ("auto-pipeline", "meta",
     _META_SKILLS_BASE / "auto-pipeline" / "SKILL.md",
     "domains/writing/meta/skills/auto-pipeline"),
    ("interactive-gating", "meta",
     _META_SKILLS_BASE / "interactive-gating" / "SKILL.md",
     "domains/writing/meta/skills/interactive-gating"),
]

# A 类 description（从 v1 subagents.py build_description 字面量提取）
_DESCRIPTIONS: dict[str, str] = {
    "storybuilding": (
        "适用：需要构建或扩展小说故事世界时调用——包括人物、世界观、"
        "故事核心、故事线（含事件组）。"
        "双层架构：storyline.md 留故事核心+故事线一览表（索引），"
        "每条故事线详情（含事件组）拆到 storyline/S{XX}-{名}.md，一条一个文件。"
        "事件以事件组为单位插入，按三幕式比例编排。"
        "增量迭代：按人物/故事线比值分流两种互斥模式——"
        "人物充足(>3)新增一条故事线，人物不足(≤3)新增一个人物并融入现有故事、不新增故事线；"
        "每次调用只执行一种模式，可循环多次调用。"
        "内置统一评估：产出后调用 evolution 评估跨维度一致性，单次评估修订（仅 1 次）。"
        "委托时必须说明：使用初构还是增量 Skill、本轮焦点、用户扩展方向。"
    ),
    "detail-outline": (
        "适用：storybuilding 产出 timeline.md 后，需要把事件编排进章节时调用。"
        "每次处理 timeline 的下一批 5-8 个事件，自主决定分几章、每章几事件，"
        "写入 detail/chapter-XX.md 并增量更新 detail/overview.md。"
        "内置 evolution 评估循环：产出后调用评估子代理，单次评估修订。"
    ),
    "writing": (
        "适用：需要生成、追加或修订单个正文章节时调用；不用于大纲、角色或评估。"
        "每次调用只写一个章节（目标约 1000 字，允许 800-1500 字浮动），"
        "写入 chapter/chapter-XX.md。内置 evolution 审查循环：写完后调用审查子代理，"
        "单次审查修订。委托时须提供总章节数、当前章节号、本章目标、必须发生的 beat。"
    ),
    "interview": (
        "适用：需要与用户多轮对话收集创作需求时调用。"
        "通过 ask_user 工具逐项提问，按 demand.md 模板填充核心/设定/风格/约束四层维度，"
        "维度齐全后请求用户确认成型。产出 demand.md，不挂评估。"
    ),
}

# B 类 middleware_params（从 v1 build_middleware 实例化参数提取）
# workspace_path 用占位符 ${ctx.workspace_path}，loader 装配时替换
_MW_PARAMS: list[tuple[str, str, dict]] = [
    # storybuilding: StorylineSingleLineLimit + ContextAssembler(demand.md)
    ("StorylineSingleLineLimit", "storybuilding", {
        "class": "StorylineSingleLineLimitMiddleware",
        "module": "app.domains.writing.expert_agent.middleware.storyline_single_line_limit",
        "args": {"workspace_path": "${ctx.workspace_path}", "max_new_lines": 1},
    }),
    ("ContextAssembler", "storybuilding", {
        "class": "ContextAssemblerMiddleware",
        "module": "app.platform.agent.middleware.context_assembler_middleware",
        "args": {
            "workspace_root": "${ctx.workspace_path}",
            "file_paths": ["demand.md"],
            "context_label": "创作需求",
        },
    }),
    # detail-outline: ContextAssembler(全套前置文件)
    ("ContextAssembler", "detail-outline", {
        "class": "ContextAssemblerMiddleware",
        "module": "app.platform.agent.middleware.context_assembler_middleware",
        "args": {
            "workspace_root": "${ctx.workspace_path}",
            "file_paths": [
                "demand.md", "outline.md", "character/*.md", "worldview.md",
                "storyline.md", "storyline/*.md", "detail/overview.md", "detail/chapter-*.md",
            ],
        },
    }),
    # writing: ContextAssembler(写作前置)
    ("ContextAssembler", "writing", {
        "class": "ContextAssemblerMiddleware",
        "module": "app.platform.agent.middleware.context_assembler_middleware",
        "args": {
            "workspace_root": "${ctx.workspace_path}",
            "file_paths": [
                "demand.md", "outline.md", "character/*.md", "worldview.md",
                "storyline.md", "storyline/*.md", "detail/*.md",
            ],
            "context_label": "写作前置上下文",
        },
    }),
    # interview: PathGuard(仅 demand.md)
    ("FilesystemPathGuard", "interview", {
        "class": "FilesystemPathGuardMiddleware",
        "module": "app.platform.agent.middleware.path_guard_middleware",
        "args": {
            "workspace_root": "${ctx.workspace_path}",
            "allowed_write_paths": ["/demand.md"],
        },
    }),
]

# B 类 permissions（从 v1 build_permissions 提取，顺序敏感）
_PERMISSIONS: list[tuple[str, str, list[dict]]] = [
    ("storybuilding", [
        {"operations": ["read"], "paths": ["/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/character/*.md"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/worldview.md"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/storyline.md"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/storyline/*.md"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/**"], "mode": "deny"},
    ]),
    ("detail-outline", [
        {"operations": ["read"], "paths": ["/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/detail/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/**"], "mode": "deny"},
    ]),
    ("writing", [
        {"operations": ["read"], "paths": ["/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/chapter/**"], "mode": "allow"},
        {"operations": ["write"], "paths": ["/**"], "mode": "deny"},
    ]),
]

# deep subagent 装配元数据（build_deep_params，存 config 供 loader 用）
# 这不是独立 surface，而是各 deep subagent 的装配参数
_DEEP_META: dict[str, dict] = {
    "storybuilding": {
        "evaluator_kind": "storybuilding",
        "max_revisions": 1,
        "artifact_paths": ["storyline.md", "storyline", "storyline/timeline.md"],
    },
    "detail-outline": {
        "evaluator_kind": "detail-outline",
        "max_revisions": 1,
        "artifact_paths": [],
    },
    "writing": {
        "evaluator_kind": "writing",
        "max_revisions": 1,
        "artifact_paths": [],
    },
}


def _read(path: Path) -> str:
    """读文件，不存在抛 FileNotFoundError（带路径信息）。"""
    if not path.exists():
        raise FileNotFoundError(f"迁移源文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ── 导入函数 ─────────────────────────────────────────────────


def import_prompts(dry_run: bool = False) -> int:
    """A 类：导入 prompt .md。"""
    count = 0
    for name, scope, role, path in _PROMPT_SOURCES:
        content = _read(path)
        config = {"role": role, "source_file": str(path.relative_to(_PROJECT_ROOT))}
        if not dry_run:
            surface_repo.create_version(
                "prompt", name, scope, content,
                config=config, commit_message="v1 harness 迁移初始版本",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[A/prompt] %s/%s ← %s", scope, name, path.name)
    return count


def import_skills(dry_run: bool = False) -> int:
    """A 类：导入 SKILL.md。"""
    count = 0
    for name, scope, path, rel_dir in _SKILL_SOURCES:
        content = _read(path)
        config = {"rel_dir": rel_dir, "source_file": str(path.relative_to(_PROJECT_ROOT))}
        if not dry_run:
            surface_repo.create_version(
                "skill", name, scope, content,
                config=config, commit_message="v1 harness 迁移初始版本",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[A/skill] %s/%s ← %s", scope, name, path.parent.name)
    return count


def import_descriptions(dry_run: bool = False) -> int:
    """A 类：导入 description 文本（从 v1 字面量）。"""
    count = 0
    for scope, text in _DESCRIPTIONS.items():
        if not dry_run:
            surface_repo.create_version(
                "description", f"description/{scope}", scope, text.strip(),
                commit_message="v1 harness 迁移初始版本",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[A/description] %s", scope)
    return count


def import_middleware_params(dry_run: bool = False) -> int:
    """B 类：导入 middleware 参数（JSON）。"""
    count = 0
    for name, scope, params in _MW_PARAMS:
        content = json.dumps(params, ensure_ascii=False, indent=2)
        if not dry_run:
            surface_repo.create_version(
                "middleware_params", name, scope, content,
                commit_message="v1 harness 迁移初始版本",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[B/middleware_params] %s/%s (%s)", scope, name, params["class"])
    return count


def import_permissions(dry_run: bool = False) -> int:
    """B 类：导入 permissions（JSON 列表）。"""
    count = 0
    for scope, perms in _PERMISSIONS:
        content = json.dumps(perms, ensure_ascii=False, indent=2)
        if not dry_run:
            surface_repo.create_version(
                "permissions", f"permissions/{scope}", scope, content,
                commit_message="v1 harness 迁移初始版本",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[B/permissions] %s (%d rules)", scope, len(perms))
    return count


def import_deep_meta(dry_run: bool = False) -> int:
    """deep subagent 装配元数据（evaluator_kind/max_revisions/artifact_paths）。

    作为 B 类 surface 存（JSON），loader 装配 deep subagent 时读取。
    interview 不走 deep，无此项。
    """
    count = 0
    for scope, meta in _DEEP_META.items():
        content = json.dumps(meta, ensure_ascii=False, indent=2)
        if not dry_run:
            surface_repo.create_version(
                "middleware_params", f"deep_meta/{scope}", scope, content,
                commit_message="v1 harness 迁移初始版本（deep 装配元数据）",
                source="migrated", status="approved",
            )
        count += 1
        logger.info("[B/deep_meta] %s (evaluator=%s)", scope, meta["evaluator_kind"])
    return count


def import_stateful_middleware(dry_run: bool = False) -> int:
    """C 类：导入 GoalMiddleware（带 state_schema 的受限代码片段）。

    content = goal_middleware.py 全文（保持 from app...import GoalState 不变）。
    执行端 importlib 加载时在自身环境解析 GoalState（D11 进程启动加载）。
    config 记 state_schema_ref 供追溯。
    """
    content = _read(_GOAL_MW_PATH)
    config = {
        "source_file": str(_GOAL_MW_PATH.relative_to(_PROJECT_ROOT)),
        "state_schema_ref": "app.domains.writing.tools.GoalState",
        "state_channels": [
            "goal", "goal_completed", "goal_acceptance_evidence",
            "goal_completed_for_turn", "goal_output_blocked",
            "goal_output_block_count", "goal_updated_for_turn",
        ],
    }
    if not dry_run:
        surface_repo.create_version(
            "stateful_middleware", "GoalMiddleware", "meta", content,
            config=config, commit_message="v1 harness 迁移初始版本（提取即冻结，D13）",
            source="migrated", status="approved",
        )
    logger.info("[C/stateful_middleware] meta/GoalMiddleware ← %s", _GOAL_MW_PATH.name)
    return 1


# ── 主流程 ───────────────────────────────────────────────────


def reset_target_tables() -> None:
    """清空目标表（迁移是「重建基准」语义，幂等重复跑需先清空）。"""
    db.execute("DELETE FROM surface_versions")
    db.execute("DELETE FROM harness_manifests")
    logger.info("已清空 surface_versions + harness_manifests（重建基准）")


def run_migration(*, dry_run: bool = False, import_only: bool = False) -> dict:
    """执行完整迁移。

    Returns: 统计摘要 {category: count, manifest: version | None}。
    """
    if not dry_run:
        reset_target_tables()

    stats: dict[str, int] = {}
    stats["prompt"] = import_prompts(dry_run)
    stats["skill"] = import_skills(dry_run)
    stats["description"] = import_descriptions(dry_run)
    stats["middleware_params"] = import_middleware_params(dry_run)
    stats["permissions"] = import_permissions(dry_run)
    stats["deep_meta"] = import_deep_meta(dry_run)
    stats["stateful_middleware"] = import_stateful_middleware(dry_run)
    total = sum(stats.values())

    result: dict = {"surfaces_imported": total, "by_category": stats}

    if import_only or dry_run:
        result["manifest"] = None
        logger.info("迁移完成（%d surfaces）%s", total, " [dry-run]" if dry_run else " [import-only]")
        return result

    # 生成初始 production manifest
    manifest = manifest_repo.publish_production()
    if manifest is None:
        logger.error("发布 manifest 失败：无 approved surface")
        result["manifest"] = None
        result["error"] = "no approved surface"
        return result
    result["manifest"] = manifest["manifest_version"]
    logger.info("迁移完成：%d surfaces → production manifest v%s",
                total, manifest["manifest_version"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="v1 harness → surface 体系迁移")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    parser.add_argument("--import-only", action="store_true", help="只导 surface，不发布 manifest")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_migration(dry_run=args.dry_run, import_only=args.import_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("manifest") is not None or args.dry_run or args.import_only else 1


if __name__ == "__main__":
    sys.exit(main())
