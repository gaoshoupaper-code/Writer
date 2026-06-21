"""Mining 引擎（Phase 3 T3.2，D8 引入 + D12 LLM 提炼 + D15 结构化预筛+LLM确认）。

论文核心环节：把单 trace 的 badcase 聚合成「失败签名」（跨 trace 反复出现的
失败模式），只有反复出现的失败才值得动 harness（天然限频 + 高信噪比）。

三步：
  1. record_badcase：评估后写 badcase_records（D20 立即写表，不立即触发）
  2. match_signature：新 badcase 匹配已有签名（D15 结构化预筛 + LLM 确认是否同病灶）
  3. mine_signature：某维度攒够 N=10（D14）→ LLM 提炼签名文本 + 组件归因（D12/S10）

组件归因（S10）：LLM 提炼签名时同时输出该签名应改 harness 的哪个组件
（prompt/skill/middleware/subagent）+ 具体哪个，proposer 据此知道改哪里。

设计依据：设计文档 D8/D12/D14/D15 + S9/S10 + badcase 数据流。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.db as db
from app import llm

logger = logging.getLogger("monitoring.mining")

# 攒够多少条同维度 badcase 才提炼签名（D14，初始默认可调）
MINE_THRESHOLD = 10


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── T3.1：badcase 记录（D20 立即写表+延迟触发）──


def record_badcase(
    trace_id: str,
    layer: str,
    target: str,
    metric: str,
    score: float,
    evidence: str = "",
) -> dict[str, Any]:
    """评估发现 badcase 后立即记录（不触发诊断，D20）。

    写 badcase_records，signature_id 暂为 NULL（待 match_signature 填充）。
    """
    now = _now()
    cur = db.execute(
        """INSERT INTO badcase_records
           (trace_id, layer, target, metric, score, evidence, signature_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
        (trace_id, layer, target, metric, float(score), evidence, now),
    )
    return {
        "id": cur.lastrowid, "trace_id": trace_id, "layer": layer,
        "target": target, "metric": metric, "score": score,
    }


def record_badcases_from_evaluation(trace_id: str, badcase_result: dict[str, Any]) -> int:
    """从 evaluate_trace 的 badcase 结果批量记录（替代旧 diagnosis 的立即触发）。

    Args:
        trace_id: trace id
        badcase_result: evaluate_trace 返回的 badcase 字段（含 flagged_dimensions）

    Returns: 记录的 badcase 条数。
    """
    if not badcase_result.get("is_badcase"):
        return 0
    count = 0
    for flagged in badcase_result.get("flagged_dimensions", []):
        record_badcase(
            trace_id=trace_id,
            layer=flagged["layer"],
            target=flagged["target"],
            metric=flagged["metric"],
            score=flagged["score"],
            evidence=flagged.get("evidence", ""),
        )
        count += 1
    return count


# ── T3.2：签名匹配（D15 结构化预筛 + LLM 确认）──


def match_signature(
    layer: str, target: str, metric: str, evidence: str,
) -> int | None:
    """新 badcase 匹配已有失败签名（D15）。

    两步：
      1. 结构化预筛：查同 layer+target+metric 的 open 签名
      2. LLM 确认：若有候选签名，LLM 判断该 evidence 是否真属同一病灶

    Returns: 匹配到的 signature_id，或 None（无匹配，开新签名候选）。
    """
    # 1. 结构化预筛：同维度的 open 签名
    candidates = db.query_all(
        """SELECT id, signature_text, target_component, target_ref FROM failure_signatures
           WHERE layer=? AND target=? AND metric=? AND status='open'""",
        (layer, target, metric),
    )
    if not candidates:
        return None

    # 只有一个候选且无 LLM 时，直接归入（保守：单候选默认匹配）
    if len(candidates) == 1 and not llm.judge_enabled():
        return candidates[0]["id"]

    # 2. LLM 确认：判断 evidence 是否属于某候选签名的同一病灶
    if not llm.judge_enabled():
        # 无 LLM：归入第一个候选（降级）
        return candidates[0]["id"]

    sig_id = _llm_confirm_match(evidence, candidates)
    return sig_id


def _llm_confirm_match(
    evidence: str, candidates: list[dict[str, Any]],
) -> int | None:
    """LLM 判断 evidence 是否属于某候选签名的同一病灶。"""
    cand_desc = "\n".join(
        f"[{i}] 签名ID={c['id']}: {c['signature_text']}"
        for i, c in enumerate(candidates)
    )
    prompt = f"""判断下面这条新的 badcase 证据，是否属于某个已知的失败签名（同一类病灶）。

## 已知失败签名候选
{cand_desc}

## 新 badcase 证据
{evidence}

请输出一个 JSON：{{"match": <签名ID 或 null>, "reason": "<一句话>"}}
- 若新证据与某签名是同一类病灶（如都是"升级后节奏拖"），输出该签名ID
- 若是不同的失败模式，输出 null"""
    try:
        raw = llm.chat([{"role": "user", "content": prompt}])
        result = _parse_json(raw)
        return result.get("match")
    except Exception:
        logger.exception("LLM 确认签名匹配失败")
        return candidates[0]["id"]  # 降级：归入第一个


def _parse_json(raw: str) -> dict[str, Any]:
    """容错解析 LLM 返回的 JSON。"""
    import re
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return {}


# ── T3.2：签名提炼（D12 LLM 提炼 + S10 组件归因）──


def find_dims_ready_to_mine(threshold: int = MINE_THRESHOLD) -> list[dict[str, Any]]:
    """找出攒够 threshold 条「未匹配签名」badcase 的维度（待提炼）。

    返回 [{layer, target, metric, count}, ...]
    """
    rows = db.query_all(
        """SELECT layer, target, metric, count(*) AS cnt
           FROM badcase_records
           WHERE signature_id IS NULL
           GROUP BY layer, target, metric
           HAVING cnt >= ?""",
        (threshold,),
    )
    return [dict(r) for r in rows]


def mine_signature(layer: str, target: str, metric: str) -> dict[str, Any] | None:
    """对攒够阈值的维度提炼失败签名（D12 + S10）。

    喂给 LLM：该维度所有未匹配 badcase 的 evidence → 输出签名文本 + 组件归因。
    提炼后：创建 failure_signatures 记录 + 关联这些 badcase（填 signature_id）。
    """
    if not llm.judge_enabled():
        logger.warning("mine_signature 跳过：LLM 未配置")
        return None

    # 取该维度所有未匹配 badcase
    badcases = db.query_all(
        """SELECT id, trace_id, score, evidence FROM badcase_records
           WHERE layer=? AND target=? AND metric=? AND signature_id IS NULL""",
        (layer, target, metric),
    )
    if len(badcases) < MINE_THRESHOLD:
        return None

    # LLM 提炼
    evidence_list = "\n".join(
        f"- trace={b['trace_id']} score={b['score']}: {b['evidence'] or '(无证据)'}"
        for b in badcases
    )
    result = _llm_extract_signature(layer, target, metric, evidence_list)
    if result is None:
        return None

    # 创建签名记录
    now = _now()
    cur = db.execute(
        """INSERT INTO failure_signatures
           (layer, target, metric, signature_text, target_component, target_ref,
            status, badcase_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
        (
            layer, target, metric,
            result["signature_text"],
            result["target_component"],
            result["target_ref"],
            len(badcases), now, now,
        ),
    )
    sig_id = cur.lastrowid

    # 关联这些 badcase（填 signature_id）
    bc_ids = [b["id"] for b in badcases]
    _link_badcases_to_signature(bc_ids, sig_id)

    return {
        "signature_id": sig_id,
        "signature_text": result["signature_text"],
        "target_component": result["target_component"],
        "target_ref": result["target_ref"],
        "badcase_count": len(badcases),
    }


def _llm_extract_signature(
    layer: str, target: str, metric: str, evidence_list: str,
) -> dict[str, Any] | None:
    """LLM 提炼失败签名 + 组件归因（S10）。"""
    prompt = f"""你是写作 Agent 系统的失败模式分析专家。

下面是同一个评估维度（{layer}/{target}/{metric}）上，多次出现的 badcase 证据。
请归纳它们的共性失败模式，形成「失败签名」。

## badcase 证据（{metric}）
{evidence_list}

## 当前 harness 组件（可改进的对象）
- prompt: meta_system / interview_system / storybuilding_system /
  detail_outline_system / writing_system（及对应 *_evaluation 审查 prompt）
- skill: auto-pipeline / interactive-gating / storybuilding-initial /
  storybuilding-expand / detail-planning / chapter-writing
- middleware: RevisionLimitMiddleware / StorylineSingleLineLimitMiddleware /
  ContextAssemblerMiddleware / GoalMiddleware / MetaReadOnlyMiddleware
- subagent: interview / storybuilding / detail-outline / writing

请输出 JSON：
{{
  "signature_text": "<一句话描述共性失败模式，如'writing subagent 在升级后连续3+章无爽点'>",
  "target_component": "prompt 或 skill 或 middleware 或 subagent",
  "target_ref": "<具体要改的组件，如 writing_system / RevisionLimitMiddleware>",
  "root_cause": "<根因分析，为什么这个组件导致了这个失败模式>"
}}

注意：target_ref 必须是上面列出的真实组件名。"""
    try:
        raw = llm.chat([{"role": "user", "content": prompt}])
        result = _parse_json(raw)
        # 校验必要字段
        for k in ("signature_text", "target_component", "target_ref"):
            if k not in result:
                logger.warning("LLM 提炼签名缺字段 %s", k)
                return None
        return result
    except Exception:
        logger.exception("LLM 提炼失败签名失败")
        return None


def _link_badcases_to_signature(badcase_ids: list[int], signature_id: int) -> None:
    """把 badcase 关联到签名（填 signature_id）。"""
    for bc_id in badcase_ids:
        db.execute(
            "UPDATE badcase_records SET signature_id=? WHERE id=?",
            (signature_id, bc_id),
        )


# ── 后台触发入口 ────────────────────────────────────────────


def check_and_mine_all(threshold: int = MINE_THRESHOLD) -> list[dict[str, Any]]:
    """后台定期调用：检查哪些维度攒够了，提炼签名。

    Returns: 本次提炼出的签名列表。
    """
    ready = find_dims_ready_to_mine(threshold)
    signatures: list[dict[str, Any]] = []
    for dim in ready:
        try:
            sig = mine_signature(dim["layer"], dim["target"], dim["metric"])
            if sig:
                signatures.append(sig)
        except Exception:
            logger.exception("提炼签名失败 %s/%s/%s", dim["layer"], dim["target"], dim["metric"])
    return signatures


def list_signatures(status: str | None = None) -> list[dict[str, Any]]:
    """列失败签名（供 API/查看）。"""
    if status:
        rows = db.query_all(
            "SELECT * FROM failure_signatures WHERE status=? ORDER BY id DESC", (status,)
        )
    else:
        rows = db.query_all("SELECT * FROM failure_signatures ORDER BY id DESC")
    return [dict(r) for r in rows]


def list_badcases(signature_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """列 badcase（供 API/查看）。"""
    if signature_id is not None:
        rows = db.query_all(
            "SELECT * FROM badcase_records WHERE signature_id=? ORDER BY id DESC LIMIT ?",
            (signature_id, limit),
        )
    else:
        rows = db.query_all(
            "SELECT * FROM badcase_records ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [dict(r) for r in rows]
