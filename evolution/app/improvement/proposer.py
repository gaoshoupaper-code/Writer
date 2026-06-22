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

import app.core.db as db
from app.core import llm

logger = logging.getLogger("evolution.proposer")

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
    from app.improvement import harness_repo

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


# ── Phase 6：按 surface 改写（bounded change，决策 D3 + 论文路线）─────────
# 上方 generate_candidate_harness 是整体重写（Phase 4 旧机制，保留向后兼容）。
# 下方是 surface 级有界改动：每次只改一个 surface，按 surface_type 分发 prompt + 校验。


# 签名 target_component（mining LLM 产出）→ surface_type 映射
# mining 提炼出的是粗粒度组件类型，proposer 转成 surface_registry 的精确 type。
_COMPONENT_TO_SURFACE_TYPE: dict[str, str] = {
    "prompt": "prompt",
    "skill": "skill",
    # middleware 要看 target_ref 进一步判断：带 state_schema → stateful_middleware，
    # 否则 → middleware_params（参数类）。默认按 middleware_params（绝大多数 middleware 无 schema）
    "middleware": "middleware_params",
    "permissions": "permissions",
    "description": "description",
    # subagent 结构改动是 C 类（改 subagent 注册 = 可能改 schema）
    "subagent": "stateful_middleware",
}


def resolve_surface_type(signature: dict[str, Any]) -> str:
    """从失败签名解析出要改的 surface_type。

    优先级：
      1. signature.surface_type（决策 D9：签名直接带 surface_type，最精确）
      2. signature.target_component → 映射（向后兼容旧签名）
      3. 兜底 prompt（最安全的 A 类）

    对 middleware 特殊处理：若 target_ref 含 state_schema 关键词 → stateful_middleware。
    """
    # 优先用签名自带的 surface_type
    explicit = signature.get("surface_type")
    if explicit:
        from app.improvement import surface_registry
        # 校验合法（未知类型回退）
        try:
            surface_registry.get_type_def(explicit)
            return explicit
        except KeyError:
            logger.warning("签名 surface_type=%s 非法，回退到 target_component 映射", explicit)

    component = signature.get("target_component", "prompt")
    surface_type = _COMPONENT_TO_SURFACE_TYPE.get(component, "prompt")

    # middleware 特判：target_ref 提示带 state_schema 的 → C 类
    target_ref = signature.get("target_ref", "")
    if component == "middleware" and any(
        kw in target_ref for kw in ("Goal", "state_schema", "State")
    ):
        return "stateful_middleware"
    return surface_type


def generate_candidate_surface(
    signature: dict[str, Any],
    surface_type: str,
    current_content: str,
    *,
    failure_feedback: str | None = None,
) -> str | None:
    """单次调用 LLM 生成候选 surface content（bounded change）。

    按 surface_type（A/B/C 层）分发改写指令：
      - A 类（文本）：最小改动改写，保留原文意图
      - B 类（JSON 参数）：只改相关字段，保持结构
      - C 类（受限 Python）：改 middleware 代码，必须满足 state_schema 契约

    Args:
        signature: 失败签名
        surface_type: 要改的 surface 类型（见 surface_registry）
        current_content: 当前 production 该 surface 的 content
        failure_feedback: 上次失败原因（重试时填）

    Returns: 新 content（A=文本/B=JSON/C=Python），或 None。
    """
    if not llm.judge_enabled():
        logger.warning("proposer 跳过：LLM 未配置")
        return None

    prompt = _build_surface_proposer_prompt(
        signature, surface_type, current_content, failure_feedback
    )
    try:
        raw = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception:
        logger.exception("proposer 生成失败")
        return None

    from app.improvement import surface_registry
    content_kind = surface_registry.get_content_kind(surface_type)
    return _extract_surface_content(raw, content_kind)


def propose_surface_with_retry(
    signature_id: int,
    surface_type: str,
    surface_name: str,
    scope: str,
    current_content: str,
    *,
    validate_fn=None,
) -> dict[str, Any] | None:
    """带重试的完整 surface propose 流程（S14，MAX_RETRIES=3）。

    Args:
        signature_id: 失败签名 ID
        surface_type/surface_name/scope: 要改的 surface 定位
        current_content: 当前 production 该 surface 的 content
        validate_fn: 校验函数 (content → (ok, error))。
                     默认用 surface_registry 的 validator（按 content_kind 分发）。

    Returns: {content, attempts, signature_id, final_error} 或 None（全部失败）。
    """
    signature = db.query_one(
        "SELECT * FROM failure_signatures WHERE id=?", (signature_id,)
    )
    if signature is None:
        logger.error("签名不存在 %s", signature_id)
        return None

    # 默认校验：surface_registry 按 content_kind 分发
    if validate_fn is None:
        from app.improvement import surface_registry
        type_def = surface_registry.get_type_def(surface_type)

        def validate_fn(content: str) -> tuple[bool, str]:
            ok, errors = type_def.validator(content, {})
            return ok, "; ".join(errors) if errors else ""

    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(
            "proposer(surface) 第 %d/%d 次（签名 %s, %s/%s）",
            attempt, MAX_RETRIES, signature_id, surface_type, surface_name,
        )
        content = generate_candidate_surface(
            signature, surface_type, current_content, failure_feedback=last_error
        )
        if content is None:
            last_error = "proposer 未输出有效 content"
            continue

        ok, err = validate_fn(content)
        if ok:
            return {
                "content": content, "attempts": attempt,
                "signature_id": signature_id,
                "surface_type": surface_type, "surface_name": surface_name,
                "scope": scope, "final_error": None,
            }
        last_error = err
        logger.warning("proposer(surface) 第 %d 次校验失败：%s", attempt, err[:200])

    logger.error("proposer(surface) %d 次全部失败（签名 %s）", MAX_RETRIES, signature_id)
    return {
        "content": None, "attempts": MAX_RETRIES, "signature_id": signature_id,
        "surface_type": surface_type, "surface_name": surface_name,
        "scope": scope, "final_error": last_error,
    }


def save_surface_candidate(
    signature_id: int,
    surface_type: str,
    surface_name: str,
    scope: str,
    content: str,
    *,
    parent_version: int | None = None,
    proposer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 proposer 生成的候选 surface 存为新版本。

    创建 surface_versions 记录（status=draft, source=proposed）。
    parent_version 是该 surface 线的当前 production 版本号。
    """
    from app.improvement import surface_repo
    return surface_repo.create_version(
        surface_type, surface_name, scope, content,
        commit_message=f"proposer 针对 signature#{signature_id} 的候选",
        source="proposed", status="draft",
        parent_version=parent_version, signature_id=signature_id,
        proposer_meta=proposer_meta,
    )


# ── surface proposer 的 prompt 构造（按层分发）──────────────────


def _build_surface_proposer_prompt(
    signature: dict[str, Any],
    surface_type: str,
    current_content: str,
    failure_feedback: str | None,
) -> str:
    """构造 surface proposer prompt，按 surface 层（A/B/C）给不同指令。"""
    from app.improvement import surface_registry
    layer = surface_registry.get_layer(surface_type)
    content_kind = surface_registry.get_content_kind(surface_type).value

    parts = [
        "你是写作 Agent 系统的 harness 进化专家。",
        f"你的任务：根据失败签名，对「单个 {surface_type} surface」做最小改动（bounded change）。\n",
        f"## 改动对象",
        f"- surface_type: {surface_type}（{layer.value} 层，content 形态: {content_kind}）",
        f"- 目标组件: {signature.get('target_ref', '(未提供)')}",
        f"- 归属 scope: {signature.get('surface_scope', '(未提供)')}",
        "\n## 失败签名（要解决的问题）",
        f"- 描述：{signature['signature_text']}",
        f"- 根因：{signature.get('root_cause', '(未提供)')}",
        f"- 维度：{signature.get('layer')}/{signature.get('target')}/{signature.get('metric')}",
        f"- 累计 badcase 数：{signature.get('badcase_count', '?')}",
    ]
    if failure_feedback:
        parts.append(f"\n## 上次尝试失败原因（请修正）\n{failure_feedback}")

    parts.extend([
        "\n## 当前 surface 内容（在此基础上最小改动）",
        _content_fence(current_content, content_kind),
        "\n## 要求（按层不同）",
    ])
    parts.extend(_layer_requirements(layer, content_kind))
    parts.append(f"\n## 输出\n{_output_instruction(content_kind)}")
    return "\n".join(parts)


def _content_fence(content: str, content_kind: str) -> str:
    """按 content_kind 用对应代码块包裹当前内容。"""
    fence = "python" if content_kind == "python" else ""
    return f"```{fence}\n{content}\n```"


def _layer_requirements(layer, content_kind: str) -> list[str]:
    """按 surface 层给不同的改动要求。"""
    from app.improvement import surface_registry
    reqs: list[str] = []
    if layer == surface_registry.SurfaceLayer.A_TEXT:
        reqs.extend([
            "1. 只针对失败签名描述的问题做最小文本改动（不要重写整段）",
            "2. 保留原文的整体结构和意图",
            "3. 输出完整的新文本（不要只输出 diff）",
        ])
    elif layer == surface_registry.SurfaceLayer.B_PARAM:
        reqs.extend([
            "1. 只修改与失败签名相关的参数字段",
            "2. 保持 JSON 结构不变（不增删顶层字段，除非签名明确要求）",
            "3. 输出完整的 JSON（合法、可解析）",
            "4. workspace_path 等 ${ctx.xxx} 占位符保持原样，不要替换为具体值",
        ])
    elif layer == surface_registry.SurfaceLayer.C_CODE:
        reqs.extend([
            "1. 只针对失败签名描述的问题改 middleware 代码",
            "2. 必须保留 state_schema 属性（C 类 surface 的核心契约）",
            "3. 不得引入危险操作（os.system/subprocess/eval/exec/socket/直接文件写）",
            "4. 不得硬编码拒绝特定题材（会误伤）",
            "5. import 语句保持不变（执行端在自身环境加载，能解析现有依赖）",
        ])
    return reqs


def _output_instruction(content_kind: str) -> str:
    """按 content_kind 给输出格式要求。"""
    if content_kind == "python":
        return "用 ```python ... ``` 代码块包裹输出完整的 middleware 代码。"
    if content_kind == "json":
        return "用 ```json ... ``` 代码块包裹输出完整的 JSON。"
    return "直接输出完整的新文本（无需代码块包裹）。"


def _extract_surface_content(raw: str, content_kind: str) -> str | None:
    """从 LLM 输出提取 content（按 content_kind 分发）。

    - python/json: 优先匹配 ```python/```json 代码块
    - text: 直接返回原文（去掉可能的代码块包裹）
    """
    import re
    if content_kind in ("python", "json"):
        # 优先匹配带语言标签的代码块
        match = re.search(rf"```{content_kind}\s*\n(.*?)```", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 退化：匹配任意代码块
        match = re.search(r"```\s*\n(.*?)```", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 最后：python 若整个输出像代码
        if content_kind == "python" and ("class " in raw or "def " in raw):
            return raw.strip()
        # json 若整个输出是 JSON
        if content_kind == "json" and raw.strip().startswith("{"):
            return raw.strip()
        return None
    # text：去掉可能的代码块包裹，返回原文
    stripped = raw.strip()
    # 去掉 ```\n...\n``` 包裹（LLM 有时多此一举）
    match = re.match(r"^```\w*\s*\n(.*?)```\s*$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped
