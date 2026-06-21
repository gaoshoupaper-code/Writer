"""proposer（Phase 4 T4.1，D4 任意代码 + S14 失败重试3次）。

读失败签名 + 当前 production harness 代码 → LLM 生成候选 harness.py。
生成的代码是完整 WriterHarness 实现（契约化 Python，D16）。

S14 失败重试：proposer 生成的代码首次失败常见（语法错/契约违反）。
每次重试带上次失败原因（HarnessLoadError/static_check 错误）反馈给 LLM 修正。

注意：proposer 产出的是「代码」，不是 prompt 文本。这要求 proposer 用强模型
（Claude/GPT-4 级），弱模型生成可用 Python 成功率极低。proposer 模型选型是
隐性成本，应与 judge 模型分开配置（避免互相影响）。

设计依据：设计文档 D4/S14 + harness 基类契约 + 一次性重写迁移边界。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.db as db
from app import llm

logger = logging.getLogger("monitoring.proposer")

# 失败重试上限（S14）
MAX_RETRIES = 3

# harness 契约说明（喂给 proposer，让它知道必须实现什么）
HARNESS_CONTRACT = """## WriterHarness 契约（必须满足）

生成的 harness.py 必须定义一个 WriterHarness 子类，实现以下抽象方法：
- build_system_prompt(self, ctx) -> str
- build_skills(self, ctx) -> list[str]（SKILL.md 目录路径）
- build_middleware(self, ctx) -> list（返回 AgentMiddleware 实例列表）
- build_tools(self, ctx) -> list（默认空，可覆盖）
- build_subagents(self, ctx) -> list（返回 SubagentHarness 实例列表）

ctx 是 HarnessContext，含 workspace_path/trace_id/owner_id/workspace_id/
meta_style/storybuilding_style/detail_outline_style/writing_style。

可用 import：
- from app.platform.harness import WriterHarness, HarnessContext, SubagentHarness
- from app.platform.prompt import load_prompt
- from app.platform.agent.middleware import (各中间件)
- from app.platform.agent.runtime import FilesystemPermission
- from app.domains.writing.middleware import GoalMiddleware, MetaReadOnlyMiddleware
- from app.domains.writing.expert_agent.types import apply_style_suffix
"""

# 当前 production harness 代码（v1，作为 proposer 改的基础）
# 实际应从 harness_repo 读当前 production 版本的代码，这里用 v1 占位
_DEFAULT_BASE_CODE_NOTE = "（从 harness_repo 取当前 production 版本的 harness.py）"


def generate_candidate_harness(
    signature: dict[str, Any],
    current_code: str,
    *,
    failure_feedback: str | None = None,
) -> str | None:
    """单次调用 LLM 生成候选 harness 代码。

    Args:
        signature: 失败签名（含 signature_text/target_component/target_ref/root_cause）
        current_code: 当前 production harness 的完整代码（改的基础）
        failure_feedback: 上次失败的原因（重试时填，首次为 None）

    Returns: 完整的候选 harness.py 代码，或 None（失败）。
    """
    if not llm.judge_enabled():
        logger.warning("proposer 跳过：LLM 未配置")
        return None

    prompt = _build_proposer_prompt(signature, current_code, failure_feedback)
    try:
        raw = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,  # 略高温度增加多样性
        )
        code = _extract_code(raw)
        if code and "class " in code and "WriterHarness" in code:
            return code
        logger.warning("proposer 输出无有效 harness 类定义")
        return None
    except Exception:
        logger.exception("proposer 生成失败")
        return None


def propose_with_retry(
    signature_id: int,
    current_code: str,
    *,
    validate_fn=None,
) -> dict[str, Any] | None:
    """带重试的完整 propose 流程（S14）。

    Args:
        signature_id: 失败签名 ID
        current_code: 当前 production harness 代码
        validate_fn: 校验函数（code → (ok, error)），用于检测生成的代码是否可用。
                     实际接入时传 worker.load_harness_instance + static_check。

    Returns: {code, attempts, signature_id, final_error} 或 None（全部失败）。
    """
    signature = db.query_one(
        "SELECT * FROM failure_signatures WHERE id=?", (signature_id,)
    )
    if signature is None:
        logger.error("签名不存在 %s", signature_id)
        return None

    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("proposer 第 %d/%d 次尝试（签名 %s）", attempt, MAX_RETRIES, signature_id)
        code = generate_candidate_harness(
            signature, current_code, failure_feedback=last_error
        )
        if code is None:
            last_error = "proposer 未输出有效代码"
            continue

        # 校验（若有 validate_fn）
        if validate_fn is not None:
            ok, err = validate_fn(code)
            if ok:
                return {
                    "code": code, "attempts": attempt,
                    "signature_id": signature_id, "final_error": None,
                }
            last_error = err
            logger.warning("proposer 第 %d 次校验失败：%s", attempt, err[:200])
        else:
            # 无校验函数：生成即接受（供测试/调试用）
            return {
                "code": code, "attempts": attempt,
                "signature_id": signature_id, "final_error": None,
            }

    logger.error("proposer %d 次全部失败（签名 %s）", MAX_RETRIES, signature_id)
    return {"code": None, "attempts": MAX_RETRIES, "signature_id": signature_id, "final_error": last_error}


def _build_proposer_prompt(
    signature: dict[str, Any], current_code: str, failure_feedback: str | None,
) -> str:
    """构造 proposer prompt。"""
    parts = [
        "你是写作 Agent 系统的 harness 进化专家。",
        "你的任务：根据失败签名，修改当前 harness 代码，生成一个改进的候选 harness。\n",
        HARNESS_CONTRACT,
        "\n## 失败签名（要解决的问题）",
        f"- 描述：{signature['signature_text']}",
        f"- 应改组件：{signature['target_component']} → {signature['target_ref']}",
        f"- 根因：{signature.get('root_cause', '(未提供)')}",
        f"- 维度：{signature['layer']}/{signature['target']}/{signature['metric']}",
        f"- 累计 badcase 数：{signature.get('badcase_count', '?')}",
    ]
    if failure_feedback:
        parts.append(f"\n## 上次尝试失败原因（请修正）\n{failure_feedback}")

    parts.extend([
        "\n## 当前 harness 代码（在此基础上修改）",
        "```python",
        current_code,
        "```",
        "\n## 要求",
        "1. 输出一个完整的 harness.py 文件（可被 importlib 加载）",
        "2. 只针对失败签名描述的问题做最小改动（不要无关重构）",
        "3. 必须满足 WriterHarness 契约（实现所有抽象方法）",
        "4. 用 ```python ... ``` 代码块包裹输出",
    ])
    return "\n".join(parts)


def _extract_code(raw: str) -> str | None:
    """从 LLM 输出提取 python 代码块。"""
    import re
    # 优先匹配 ```python ... ```
    match = re.search(r"```python\s*\n(.*?)```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    # 退化：匹配 ``` ... ```
    match = re.search(r"```\s*\n(.*?)```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    # 最后：若整个输出看起来像代码（含 class + import）
    if "class " in raw and ("import" in raw or "def " in raw):
        return raw.strip()
    return None


def save_candidate(
    signature_id: int,
    code: str,
    harnesses_root,
    *,
    parent_version: int,
    proposer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 proposer 生成的候选 harness 存为新版本。

    写代码文件 + 创建 harness_versions 记录（status=draft, label=candidate）。
    """
    from pathlib import Path
    from app import harness_repo

    # 临时版本号（实际 version 由 create_version 自增）
    # 先创建记录拿 id，再写文件用 id 命名目录
    version = harness_repo.create_version(
        code_path="(pending)",  # 占位，写完文件后更新
        parent_version=parent_version,
        source="proposed",
        labels=["candidate"],
        signature_id=signature_id,
        proposer_meta=proposer_meta,
        status="draft",
    )
    # 写代码文件
    code_path = harness_repo.write_harness_code(
        Path(harnesses_root), code, version["id"]
    )
    # 更新 code_path
    db.execute(
        "UPDATE harness_versions SET code_path=? WHERE id=?",
        (str(code_path), version["id"]),
    )
    return harness_repo.get_version_by_id(version["id"])  # type: ignore[return-value]
