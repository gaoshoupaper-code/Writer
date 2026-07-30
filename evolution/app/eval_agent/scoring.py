"""28 维五级锚点评估打分（第三期，2026-07）。

编排（按任务契约自适应启用评分组，每组 1 次 LLM 调用）：
  - 通用五维（所有任务适用）
  - 玄幻故事构建六维（有 storybuilding 交付物时）
  - 玄幻详细大纲六维（有 detail-outline 交付物时）
  - 玄幻正文十一维（有 writing 交付物时）

每次调用：rubric prompt + 交付物文本 → LLM → 解析 JSON 五级锚定分（1-5）。
幂等：evaluation_runs 有 done 记录则跳过（同 trace 不重评）。

设计依据：.claude/md/20260726_214821_trace证据管线.md § 评估维度矩阵
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.eval_agent import eval_extractor
from app.core import llm
from app.eval_agent.rubrics import xianxia as rubric
from app.trace.observers import TraceLlmObserver, elapsed_ms

logger = logging.getLogger("evolution.eval_agent.scoring")

# ── 内容评分预算（DEC-002 / NFR-001，冻结数值，执行模型不得改）──────────
# 并发上限 2、单组单次 60s、失败组最多重试 1 次、内容评分总墙钟预算 150s。
CONTENT_MAX_CONCURRENCY = 2
CONTENT_GROUP_TIMEOUT_S = 60.0
CONTENT_GROUP_MAX_ATTEMPTS = 2  # 首次 + 至多 1 次重试
CONTENT_TOTAL_BUDGET_S = 150.0


def evaluate_trace(trace_id: str) -> dict[str, Any] | None:
    """对一个 trace 跑完整 28 维评估。

    Returns: 评估结果 dict（含各组分数/badcase 标记/校准状态），失败/跳过返回 None。
    幂等：evaluation_runs 有 done 记录则跳过。
    """
    if not llm.judge_enabled():
        logger.warning("evaluate_trace 跳过：LLM 未配置")
        return None

    # 幂等：已评估过则跳过
    existing = db.query_one("SELECT status FROM evaluation_runs WHERE trace_id = ?", (trace_id,))
    if existing and existing["status"] == "done":
        return None

    # 标记进行中
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO evaluation_runs (trace_id, status, started_at) VALUES (?, 'pending', ?)
           ON CONFLICT(trace_id) DO UPDATE SET status='pending', started_at=excluded.started_at, error=NULL""",
        (trace_id, now),
    )

    try:
        # 1. 提取交付物 + 判断启用哪些评分组
        deliveries = eval_extractor.extract_deliveries(trace_id)
        groups = rubric.applicable_groups(deliveries)

        # 2. 按组评估（每组 1 次 LLM 调用）
        group_results: dict[str, dict[str, Any]] = {}
        for group_name, dimensions in groups.items():
            result = _evaluate_group(trace_id, group_name, dimensions, deliveries)
            group_results[group_name] = result

        # 3. 落盘
        _save_scores(trace_id, group_results)

        # 4. badcase 判定
        badcase = _detect_badcase(group_results)

        db.execute(
            "UPDATE evaluation_runs SET status='done', finished_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), trace_id),
        )

        # 5. 反思归纳（badcase 时自动归纳，异常不阻断）
        if badcase.get("is_badcase"):
            try:
                from app.reflection.extractor import extract_from_eval
                extract_from_eval(trace_id, {"badcase": badcase})
            except Exception:
                logger.warning("反思归纳异常（不阻断评估）trace=%s", trace_id, exc_info=True)

        return {
            "groups": group_results,
            "badcase": badcase,
            "calibration": rubric.CALIBRATION_STATUS,
            "rubric_version": rubric.RUBRIC_VERSION,
        }
    except Exception as exc:
        logger.exception("evaluate_trace 失败 %s", trace_id)
        db.execute(
            "UPDATE evaluation_runs SET status='error', error=?, finished_at=? WHERE trace_id=?",
            (str(exc)[:500], datetime.now(UTC).isoformat(), trace_id),
        )
        return None


def _evaluate_group(
    trace_id: str,
    group_name: str,
    dimensions: list[dict],
    deliveries: dict[str, dict[str, str]],
    observer: TraceLlmObserver | None = None,
) -> dict[str, Any]:
    """评估单个评分组（1 次 LLM 调用）。

    通用五维基于全部交付物拼接文本；其他组基于对应 agent 的交付物。
    observer（DEC-001 / FR-003）：传入时本次 LLM 调用写进评估 Trace。
    """
    artifact_text = _get_group_artifact_text(group_name, deliveries)
    if not artifact_text:
        label = rubric.GROUP_LABELS.get(group_name, group_name)
        return {"skipped": True, "reason": f"无 {label} 交付物"}

    dim_keys = [d["key"] for d in dimensions]
    rubric_prompt = rubric.build_group_rubric_prompt(group_name, dimensions)
    output_format = rubric.build_output_format(dim_keys)

    messages = [
        {"role": "system", "content": rubric_prompt + output_format},
        {"role": "user", "content": f"## 待评估产物\n\n{artifact_text}"},
    ]
    raw = llm.chat(messages, trace=observer, phase=f"content_score:{group_name}")
    result = _parse_response(raw)

    scores = result.get("scores", {})
    # 过滤 score=0（无法判断）的维度，不计入组分
    valid_scores = {k: v for k, v in scores.items() if isinstance(v, (int, float)) and v > 0}
    group_score = (
        round(sum(valid_scores.values()) / len(valid_scores), 2)
        if valid_scores else 0
    )

    return {
        "skipped": False,
        "scores": scores,
        "group_score": group_score,
        "valid_dim_count": len(valid_scores),
        "total_dim_count": len(dim_keys),
        "evidence": result.get("evidence", ""),
        "verdict": result.get("verdict", "review"),
    }


def _get_group_artifact_text(
    group_name: str,
    deliveries: dict[str, dict[str, str]],
) -> str:
    """获取某组的评估产物文本。

    通用五维：拼接所有可用交付物（demand + 设定 + 大纲 + 正文摘要）。
    其他组：取对应 agent 的交付物。
    """
    if group_name == "general":
        # 通用五维基于全部交付物，但截断控制总长度
        parts: list[str] = []
        for agent in ("interview", "storybuilding", "detail-outline", "writing"):
            files = deliveries.get(agent, {})
            agent_text = "\n\n---\n\n".join(
                f"## 文件: {path}\n\n{content[:2000]}"
                for path, content in sorted(files.items())
            )
            if agent_text:
                parts.append(f"### {agent} 交付物\n{agent_text[:3000]}")
        return "\n\n".join(parts)

    # 其他组：取对应 agent 交付物
    agent_map = {"storybuilding": "storybuilding", "outline": "detail-outline", "body": "writing"}
    agent = agent_map.get(group_name)
    if not agent:
        return ""
    files = deliveries.get(agent, {})
    if not files:
        return ""
    return "\n\n---\n\n".join(
        f"## 文件: {p}\n\n{c[:6000]}" for p, c in sorted(files.items())
    )


def _parse_response(raw: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块、提取首个 JSON 对象）。"""
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
    raise ValueError(f"无法解析 LLM 返回为 JSON: {raw[:200]}")


def _save_scores(trace_id: str, group_results: dict[str, dict[str, Any]]) -> None:
    """写 evaluation_scores 表（layer=组名，target=组名，metric=维度名）。"""
    now = datetime.now(UTC).isoformat()
    rows: list[tuple[Any, ...]] = []

    for group_name, result in group_results.items():
        if result.get("skipped"):
            continue
        evidence = result.get("evidence", "")
        for metric, score in result.get("scores", {}).items():
            rows.append((
                trace_id, group_name, group_name, metric,
                float(score) if isinstance(score, (int, float)) else 0.0,
                evidence, now,
            ))

    if rows:
        db.executemany(
            """INSERT INTO evaluation_scores
               (trace_id, layer, target, metric, score, evidence, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def _detect_badcase(group_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """badcase 判定：任一维度 < 3（五级制阈值）即 badcase。

    score=0（无法判断）不计入 badcase 判定（证据不足不等于质量差）。
    """
    threshold = rubric.BADCASE_THRESHOLD
    flagged: list[dict[str, Any]] = []

    for group_name, result in group_results.items():
        if result.get("skipped"):
            continue
        evidence = result.get("evidence", "")
        for metric, score in result.get("scores", {}).items():
            if not isinstance(score, (int, float)):
                continue
            if score == 0:
                continue  # 无法判断不计入
            if score < threshold:
                flagged.append({
                    "group": group_name,
                    "metric": metric,
                    "score": float(score),
                    "threshold": threshold,
                    "evidence": evidence,
                })

    return {
        "is_badcase": len(flagged) > 0,
        "flagged_dimensions": flagged,
    }


def _deliveries_from_facts(facts: dict[str, Any]) -> dict[str, dict[str, str]]:
    """从卷宗 facts 还原 scoring 期望的 deliveries 格式（阶段 C：取 content_frozen）。

    facts.deliveries 是 {agent: {path: {content_frozen,...}}}，scoring 期望
    {agent: {path: content_str}}。
    """
    frozen = facts.get("deliveries") or {}
    deliveries: dict[str, dict[str, str]] = {}
    for agent, files in frozen.items():
        agent_files: dict[str, str] = {}
        for path, meta in files.items():
            content = meta.get("content_frozen") if isinstance(meta, dict) else meta
            if content:
                agent_files[path] = content
        if agent_files:
            deliveries[agent] = agent_files
    return deliveries


def _content_input_hash(deliveries: dict[str, dict[str, str]], eval_id: str) -> str:
    """内容评分的稳定计算身份（FR-003 兼容性 / 防误用旧缓存）。

    由 eval_id + 适用组结构 + 冻结正文 hash + rubric 版本共同决定。rubric 版本或
    输入 hash 变化时产生新身份，不会误用旧缓存。
    """
    parts: list[str] = [eval_id, rubric.RUBRIC_VERSION]
    for agent in sorted(deliveries):
        for path in sorted(deliveries[agent]):
            parts.append(f"{agent}/{path}:{hashlib.sha256(deliveries[agent][path].encode('utf-8')).hexdigest()[:16]}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ── 组级结果缓存（DEC-002：已成功/终态结果不重算）──────────────────────
# 按 (eval_id, computation_hash, group_name) 缓存组级结果。同一评估 Agent 重复
# 调用 get_content_score 只复用结果，不启动第二轮整组计算（修复 184.8s 整组重跑）。
# 进程内字典，生命周期与单次评估 session 一致；clear_content_cache 在 session 结束清理。
_group_result_cache: dict[tuple[str, str, str], dict[str, Any]] = {}


def clear_content_cache(eval_id: str | None = None) -> None:
    """清理组级结果缓存。传 eval_id 只清该评估；否则全清。"""
    if eval_id is None:
        _group_result_cache.clear()
        return
    for key in [k for k in _group_result_cache if k[0] == eval_id]:
        _group_result_cache.pop(key, None)


def _group_terminal(result: dict[str, Any]) -> str | None:
    """判定组结果是否已终态（completed/failed/skipped）。非终态返回 None（需计算）。"""
    status = result.get("status")
    if status in ("completed", "failed", "skipped"):
        return status
    return None


async def _evaluate_group_with_budget(
    *,
    eval_id: str,
    comp_hash: str,
    group_name: str,
    dimensions: list[dict],
    deliveries: dict[str, dict[str, str]],
    trace_id: str,
    observer: TraceLlmObserver | None,
    deadline: float,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """单评分组的受预算执行：并发≤上限、单组超时、失败组最多重试一次、结果缓存。

    返回机器可读的组级结果（CON-003：错误不再伪装成工具成功）：
      {"status": "completed"|"failed"|"skipped", "group": group_name,
       "scores":..., "group_score":..., "attempts": n, "duration_ms": ms, "error": str|None}
    已成功/终态组直接复用缓存（成功组重算次数 = 0）。
    """
    cache_key = (eval_id, comp_hash, group_name)
    cached = _group_result_cache.get(cache_key)
    if cached and _group_terminal(cached):
        # 复用已终态结果（含成功与已判失败的组）；成功组不再调用。
        return {**cached, "reused": True}

    # skipped（无交付物）不走 LLM，直接终态并缓存。
    artifact_text = _get_group_artifact_text(group_name, deliveries)
    if not artifact_text:
        label = rubric.GROUP_LABELS.get(group_name, group_name)
        result: dict[str, Any] = {
            "status": "skipped", "group": group_name, "reason": f"无 {label} 交付物",
            "attempts": 0, "duration_ms": 0.0, "error": None,
        }
        _group_result_cache[cache_key] = result
        return result

    started = time.perf_counter()
    attempts = 0
    last_error: str | None = None
    group_payload: dict[str, Any] | None = None

    while attempts < CONTENT_GROUP_MAX_ATTEMPTS:
        # 预算耗尽则停止重试（DEC-002：重试服从 150s 总预算）。
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            last_error = "内容评分总预算耗尽"
            break
        per_call_timeout = min(CONTENT_GROUP_TIMEOUT_S, remaining)

        async with semaphore:
            if observer:
                observer.phase_start(
                    f"content_score:{group_name}",
                    message=f"评分组 {group_name} 第 {attempts + 1} 次调用",
                )
            call_started = time.perf_counter()
            try:
                attempts += 1
                group_payload = await asyncio.wait_for(
                    asyncio.to_thread(
                        _evaluate_group, trace_id, group_name, dimensions, deliveries, observer,
                    ),
                    timeout=per_call_timeout,
                )
                # 成功：缓存并返回。
                result = {
                    "status": "completed",
                    "group": group_name,
                    "scores": group_payload.get("scores", {}),
                    "group_score": group_payload.get("group_score", 0),
                    "valid_dim_count": group_payload.get("valid_dim_count", 0),
                    "total_dim_count": group_payload.get("total_dim_count", 0),
                    "evidence": group_payload.get("evidence", ""),
                    "verdict": group_payload.get("verdict", "review"),
                    "attempts": attempts,
                    "duration_ms": elapsed_ms(started),
                    "error": None,
                }
                _group_result_cache[cache_key] = result
                if observer:
                    observer.phase_end(
                        f"content_score:{group_name}", duration_ms=elapsed_ms(call_started),
                        status="completed", attempts=attempts,
                    )
                return result
            except asyncio.TimeoutError:
                last_error = f"评分组 {group_name} 超时（>{per_call_timeout:.0f}s）"
                if observer:
                    observer.phase_fail(
                        f"content_score:{group_name}", error=last_error,
                        duration_ms=elapsed_ms(call_started),
                    )
                logger.warning("%s: %s", last_error, trace_id)
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                if observer:
                    observer.phase_fail(
                        f"content_score:{group_name}", error=last_error,
                        duration_ms=elapsed_ms(call_started),
                    )
                logger.warning("评分组 %s 调用失败 trace=%s: %s", group_name, trace_id, exc)
        # 失败组在剩余预算内重试一次；循环条件 + 预算检查控制上限。

    # 组最终失败（超时/异常在首次 + 至多 1 次重试后仍未成功）。
    result = {
        "status": "failed", "group": group_name, "scores": {},
        "group_score": 0, "attempts": attempts,
        "duration_ms": elapsed_ms(started), "error": last_error,
    }
    _group_result_cache[cache_key] = result
    return result


async def evaluate_content_groups(
    facts: dict[str, Any],
    trace_id: str,
    *,
    eval_id: str,
    observer: TraceLlmObserver | None = None,
) -> dict[str, Any]:
    """从证据卷宗 facts 评估内容质量（阶段 C + DEC-002 重写）。

    与旧 evaluate_from_facts 的关键差异（FR-003 / NFR-001）：
      - 最多两组并发（Semaphore(2)），而非串行；
      - 每组单次调用 ≤ 60s，失败组最多重试一次，服从 150s 总预算；
      - 已成功/终态组复用缓存，成功组重算次数精确为 0；
      - 返回机器可读组级状态（completed/failed/skipped），不再把错误当字符串成功；
      - 任一必需组最终失败或总预算耗尽 → 整体 failed 且不封存 partial（CON-003/EDGE-002）。

    返回结构兼容旧 {groups/badcase/calibration/rubric_version}，额外含：
      - groups[group_name].status / .attempts / .duration_ms / .error（机器可读）
      - computation_hash（稳定计算身份，供诊断）
      - failed_groups（明确失败组列表）
      - complete（bool，是否全部必需组 completed）
    """
    if not llm.judge_enabled():
        logger.warning("evaluate_content_groups 跳过：LLM 未配置")
        return {"skipped": True, "reason": "LLM 未配置", "complete": False}

    deliveries = _deliveries_from_facts(facts)
    if not deliveries:
        return {"skipped": True, "reason": "卷宗无冻结交付物", "complete": False}

    comp_hash = _content_input_hash(deliveries, eval_id)
    groups = rubric.applicable_groups(deliveries)
    deadline = time.perf_counter() + CONTENT_TOTAL_BUDGET_S
    semaphore = asyncio.Semaphore(CONTENT_MAX_CONCURRENCY)

    if observer:
        observer.phase_start(
            "content_scoring",
            message=f"内容评分（{len(groups)} 组，并发≤{CONTENT_MAX_CONCURRENCY}，预算 {CONTENT_TOTAL_BUDGET_S:.0f}s）",
            computation_hash=comp_hash,
        )
    t_total = time.perf_counter()

    # 所有组并发（受 semaphore 限制为 2），每组各自管理超时/重试/预算。
    tasks = [
        asyncio.create_task(
            _evaluate_group_with_budget(
                eval_id=eval_id, comp_hash=comp_hash, group_name=name,
                dimensions=dims, deliveries=deliveries, trace_id=trace_id,
                observer=observer, deadline=deadline, semaphore=semaphore,
            ),
            name=f"content-score-{name}",
        )
        for name, dims in groups.items()
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    group_results: dict[str, dict[str, Any]] = {}
    failed_groups: list[str] = []
    for name, raw in zip(groups.keys(), raw_results):
        if isinstance(raw, BaseException):
            # gather 的异常兜底（不应发生，_evaluate_group_with_budget 已内部捕获）。
            group_results[name] = {
                "status": "failed", "group": name, "scores": {}, "group_score": 0,
                "attempts": 0, "duration_ms": 0.0,
                "error": f"{raw.__class__.__name__}: {raw}",
            }
            failed_groups.append(name)
        else:
            group_results[name] = raw
            if raw.get("status") == "failed":
                failed_groups.append(name)

    total_ms = elapsed_ms(t_total)
    budget_remaining = max(0.0, deadline - time.perf_counter())
    complete = not failed_groups  # 全部必需组 completed（skipped 视为已处理，非失败）

    if observer:
        observer.phase_end(
            "content_scoring", duration_ms=total_ms,
            complete=complete, failed_groups=failed_groups,
            budget_remaining_s=round(budget_remaining, 1),
        )

    # badcase 判定只看 completed 组的分数（failed/skipped 不参与）。
    scored_only = {
        name: r for name, r in group_results.items()
        if r.get("status") == "completed"
    }
    badcase = _detect_badcase(scored_only)

    return {
        "groups": group_results,
        "badcase": badcase,
        "calibration": rubric.CALIBRATION_STATUS,
        "rubric_version": rubric.RUBRIC_VERSION,
        "computation_hash": comp_hash,
        "failed_groups": failed_groups,
        "complete": complete,
        "total_duration_ms": total_ms,
        "budget_remaining_s": round(budget_remaining, 1),
    }


def evaluate_from_facts(facts: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """[已弃用，保留兼容] 从卷宗 facts 评估内容质量（同步、串行、无预算）。

    新评估流程请用 evaluate_content_groups（async，DEC-002）。本函数仅留给
    未接 event loop 的旧调用方（benchmark/promote 走 evaluate_trace，不走这里），
    内部退化为串行单次调用，不享受并发/预算/缓存。
    """
    if not llm.judge_enabled():
        return {"skipped": True, "reason": "LLM 未配置", "complete": False}

    deliveries = _deliveries_from_facts(facts)
    if not deliveries:
        return {"skipped": True, "reason": "卷宗无冻结交付物", "complete": False}

    try:
        groups = rubric.applicable_groups(deliveries)
        group_results: dict[str, dict[str, Any]] = {}
        for group_name, dimensions in groups.items():
            group_results[group_name] = _evaluate_group(trace_id, group_name, dimensions, deliveries)
        badcase = _detect_badcase(group_results)
        return {
            "groups": group_results,
            "badcase": badcase,
            "calibration": rubric.CALIBRATION_STATUS,
            "rubric_version": rubric.RUBRIC_VERSION,
            "complete": True,
        }
    except Exception as exc:
        logger.exception("evaluate_from_facts 失败 trace=%s", trace_id)
        return {"error": str(exc), "complete": False}


__all__ = [
    "evaluate_trace",
    "evaluate_from_facts",
    "evaluate_content_groups",
    "clear_content_cache",
]
