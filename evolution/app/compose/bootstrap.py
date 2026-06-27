"""bootstrap —— 从现有 assemble 硬编码生成 v1 HarnessConfig（Task 1.3）。

一次性迁移脚本：读 harnesses/current/ 包目录，把现有硬编码的 middleware 顺序、
subagent 配置、prompt 引用、skills 路径翻译成 HarnessConfig JSON。

生成的 v1 config 是 compose 体系的起点——之后 evolution 通过 edit 指令修改它，
executor 通过读它来 assemble。

与现有 assemble（harnesses/current/__init__.py）的对应关系：
  - meta_middleware 列表 → meta_pipeline.processors
  - meta system_prompt → meta_pipeline.slots.system_prompt
  - meta skills → meta_pipeline.slots.skills
  - 每个 subagent 的 build_* → subagents.{name}
  - subagent 的 context_file_paths / style_suffix 留 build_* 专属逻辑（D12a，不进 config）

注意：本文件提取的是「静态可配置部分」。subagent 的专属逻辑（context 注入路径、
style suffix、storyline 约束）保留在 build_* 函数（决策 D12a），不强行配置化。

设计依据：设计文档 D12a + Task 1.3。
"""
from __future__ import annotations

from pathlib import Path

from . import config as cfg

# harnesses/current 包目录
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "harnesses" / "current"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
SKILLS_DIR = PACKAGE_DIR / "skills"


def _read_prompt(name: str) -> str:
    """读包内 prompts/<name>.md 文本。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _skill_rel_paths(scope: str) -> list[str]:
    """取某 scope 的 skill 相对路径（相对包根，用正斜杠）。"""
    base = SKILLS_DIR
    if scope == "meta":
        paths = [base / "meta" / "auto-pipeline", base / "meta" / "interactive-gating"]
    elif scope == "storybuilding":
        paths = [base / "storybuilding-initial", base / "storybuilding-expand"]
    elif scope == "detail_outline":
        paths = [base / "detail_outline" / "detail-planning"]
    elif scope == "writing":
        paths = [base / "writing" / "chapter-writing"]
    else:
        return []
    return [str(p.relative_to(PACKAGE_DIR)).replace("\\", "/") for p in paths]


def build_v1_config() -> dict:
    """生成 v1 HarnessConfig。

    从现有 assemble 的硬编码逻辑提取。改 assemble 时同步更新本函数。

    Returns:
        v1 HarnessConfig dict（已 validate）
    """
    config = cfg.empty_config()

    # ── meta_pipeline ──────────────────────────────────────────
    meta = config["meta_pipeline"]

    # 内容型 slot：system_prompt + skills
    meta["slots"]["system_prompt"] = cfg.make_prompt_slot(_read_prompt("meta_system"))
    meta["slots"]["skills"] = cfg.make_skills_slot(_skill_rel_paths("meta"))

    # meta middleware（顺序对应 assemble 里的 meta_middleware 列表）
    # 注意：TraceMiddleware 不进 config——它由 ctx 注入（运行时值，D13a），
    # assemble 在实例化时按 ctx.trace_recorder 动态插入。
    meta["processors"] = [
        cfg.make_processor("before_agent", "error_recovery", "ErrorRecoveryMiddleware"),
        cfg.make_processor("before_model", "meta_readonly", "MetaReadOnlyMiddleware"),
        cfg.make_processor("before_model", "path_guard", "FilesystemPathGuardMiddleware"),
        cfg.make_processor("wrap_model_call", "file_write_serialize", "FileWriteSerializeMiddleware"),
        # GoalMiddleware 带工具注册（GoalState schema），C 类（改 state_schema）
        cfg.make_processor("before_model", "goal", "GoalMiddleware"),
    ]

    # ── subagents ─────────────────────────────────────────────
    subs = config["subagents"]

    # 通用 middleware 工厂对应的 processor 列表（每个 subagent 共享的基础三件套）
    # 对应 assemble 里 middleware_factory 的输出：
    #   [ErrorRecovery, PathGuard, FileWriteSerialize]
    # TraceMiddleware 同样不进 config（ctx 注入）。
    def _base_processors() -> list[dict]:
        return [
            cfg.make_processor("before_agent", "error_recovery", "ErrorRecoveryMiddleware"),
            cfg.make_processor("before_model", "path_guard", "FilesystemPathGuardMiddleware"),
            cfg.make_processor("wrap_model_call", "file_write_serialize", "FileWriteSerializeMiddleware"),
        ]

    # --- storybuilding ---
    sb = cfg.empty_pipeline()
    sb["slots"]["system_prompt"] = cfg.make_prompt_slot(_read_prompt("storybuilding_system"))
    sb["slots"]["skills"] = cfg.make_skills_slot(_skill_rel_paths("storybuilding"))
    sb["processors"] = _base_processors()
    # 专属：单次单线硬约束（StorylineSingleLineLimitMiddleware）
    sb["processors"].append(
        cfg.make_processor("wrap_tool_call", "storyline_single_line", "StorylineSingleLineLimitMiddleware",
                           {"max_new_lines": 1})
    )
    # 专属：review 修订上限（RevisionLimitMiddleware）
    sb["processors"].append(
        cfg.make_processor("wrap_tool_call", "revision_limit", "RevisionLimitMiddleware",
                           {"max_revisions": 1})
    )
    # 注意：ContextAssemblerMiddleware 不进 config——它的 file_paths 参数
    # 含运行时语义（相对 workspace_root），且 D12a 把 context 注入留 build_* 专属逻辑。
    # 如果未来要把 context 注入配置化，需单独设计 file_paths 的表达。
    subs["storybuilding"] = sb

    # --- detail_outline ---
    do = cfg.empty_pipeline()
    do["slots"]["system_prompt"] = cfg.make_prompt_slot(_read_prompt("detail_outline_system"))
    do["slots"]["skills"] = cfg.make_skills_slot(_skill_rel_paths("detail_outline"))
    do["processors"] = _base_processors()
    do["processors"].append(
        cfg.make_processor("wrap_tool_call", "revision_limit", "RevisionLimitMiddleware",
                           {"max_revisions": 1})
    )
    subs["detail_outline"] = do

    # --- writing ---
    wr = cfg.empty_pipeline()
    wr["slots"]["system_prompt"] = cfg.make_prompt_slot(_read_prompt("writing_system"))
    wr["slots"]["skills"] = cfg.make_skills_slot(_skill_rel_paths("writing"))
    wr["processors"] = _base_processors()
    wr["processors"].append(
        cfg.make_processor("wrap_tool_call", "revision_limit", "RevisionLimitMiddleware",
                           {"max_revisions": 1})
    )
    subs["writing"] = wr

    # --- interview（无 review，无专属约束）---
    iv = cfg.empty_pipeline()
    iv["slots"]["system_prompt"] = cfg.make_prompt_slot(_read_prompt("interview_system"))
    iv["slots"]["skills"] = []
    iv["processors"] = _base_processors()
    subs["interview"] = iv

    # --- general-purpose（DeepAgents 自带，无 prompt/skills，仅基础 middleware）---
    gp = cfg.empty_pipeline()
    gp["slots"]["system_prompt"] = cfg.make_prompt_slot("You are a general-purpose assistant.")
    gp["slots"]["skills"] = []
    gp["processors"] = _base_processors()
    subs["general_purpose"] = gp

    cfg.validate(config)
    return config


def main() -> None:
    """命令行入口：生成 v1 config 并打印（或重定向到文件）。"""
    config = build_v1_config()
    print(cfg.to_json(config))


if __name__ == "__main__":
    main()
