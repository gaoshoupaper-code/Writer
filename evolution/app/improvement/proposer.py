"""proposer（surface 级有界改动，决策 D3 + S14 失败重试3次）。

读失败签名 → 解析 surface_type → LLM 生成单个 surface 的候选 content。
每次只改一个 surface（bounded change），不整体重写 harness。

S14 失败重试：proposer 生成的 content 首次失败常见（语法错/契约违反）。
每次重试带上次失败原因反馈给 LLM 修正。

注意：proposer 产出的是「单个 surface 的 content」（文本/JSON/受限 Python），
按 surface_type 的 layer 分发 prompt + validator。

设计依据：设计文档 D3/S14 + surface 三层分层（contracts.surface_types）。
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


# ── Phase 6：按 surface 改写（bounded change，决策 D3 + 论文路线）─────────
# surface 级有界改动：每次只改一个 surface，按 surface_type 分发 prompt + 校验。


# 签名 target_component（mining LLM 产出）→ surface_type 映射
# mining 提炼出的是粗粒度组件类型，proposer 转成 contracts.surface_types 的精确 type。
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
        from contracts import surface_types
        # 校验合法（未知类型回退）
        try:
            surface_types.get_type_def(explicit)
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
        surface_type: 要改的 surface 类型（见 contracts.surface_types）
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

    from contracts import surface_types
    content_kind = surface_types.get_content_kind(surface_type)
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
                     默认用 static_check 的 validator（按 surface_type 分发）。

    Returns: {content, attempts, signature_id, final_error} 或 None（全部失败）。
    """
    signature = db.query_one(
        "SELECT * FROM failure_signatures WHERE id=?", (signature_id,)
    )
    if signature is None:
        logger.error("签名不存在 %s", signature_id)
        return None

    # 默认校验：static_check 按 surface_type 分发（validator 注册在 static_check.VALIDATOR_MAP）
    if validate_fn is None:
        from app.improvement import static_check

        def validate_fn(content: str) -> tuple[bool, str]:
            ok, errors = static_check.validate_surface(surface_type, content)
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


# ── surface proposer 的 prompt 构造（按层分发）──────────────────


def _build_surface_proposer_prompt(
    signature: dict[str, Any],
    surface_type: str,
    current_content: str,
    failure_feedback: str | None,
) -> str:
    """构造 surface proposer prompt，按 surface 层（A/B/C）给不同指令。"""
    from contracts import surface_types
    layer = surface_types.get_layer(surface_type)
    content_kind = surface_types.get_content_kind(surface_type).value

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
    from contracts import surface_types
    reqs: list[str] = []
    if layer == surface_types.SurfaceLayer.A_TEXT:
        reqs.extend([
            "1. 只针对失败签名描述的问题做最小文本改动（不要重写整段）",
            "2. 保留原文的整体结构和意图",
            "3. 输出完整的新文本（不要只输出 diff）",
        ])
    elif layer == surface_types.SurfaceLayer.B_PARAM:
        reqs.extend([
            "1. 只修改与失败签名相关的参数字段",
            "2. 保持 JSON 结构不变（不增删顶层字段，除非签名明确要求）",
            "3. 输出完整的 JSON（合法、可解析）",
            "4. workspace_path 等 ${ctx.xxx} 占位符保持原样，不要替换为具体值",
        ])
    elif layer == surface_types.SurfaceLayer.C_CODE:
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
