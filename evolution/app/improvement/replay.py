"""回放测试集管理（Phase 3 T3.2）。

职责：
  - replay_test_sets 表的 CRUD（A/B 回放的标准化创作需求集）
  - COIG-Writer 玄幻子集导入（可选，需 datasets 库）

测试集结构：[{request: 创作需求, genre: 品类}, ...]
A/B 回放（experiment.py）用测试集的 request 作为创作需求，跑 production vs candidate。

设计依据：设计文档 D（半调研半自生成）+ COIG-Writer 核实结论。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.replay")


def list_test_sets() -> list[dict[str, Any]]:
    """列出所有回放测试集。"""
    rows = db.query_all("SELECT * FROM replay_test_sets ORDER BY id DESC")
    result = []
    for r in rows:
        item = dict(r)
        item["prompts"] = json.loads(r["prompts_json"]) if r["prompts_json"] else []
        item["item_count"] = len(item["prompts"])
        result.append(item)
    return result


def get_test_set(test_set_id: int) -> dict[str, Any] | None:
    """取单个测试集（含 prompts 解析）。"""
    row = db.query_one("SELECT * FROM replay_test_sets WHERE id=?", (test_set_id,))
    if row is None:
        return None
    item = dict(row)
    item["prompts"] = json.loads(row["prompts_json"]) if row["prompts_json"] else []
    return item


def create_test_set(name: str, prompts: list[dict[str, str]], description: str = "") -> dict[str, Any]:
    """创建回放测试集。

    Args:
        name: 测试集名（唯一）
        prompts: [{request: 创作需求, genre: 品类}, ...]
        description: 描述
    """
    existing = db.query_one("SELECT id FROM replay_test_sets WHERE name=?", (name,))
    if existing:
        raise ValueError(f"测试集已存在: {name}")
    now = datetime.now(UTC).isoformat()
    cur = db.execute(
        """INSERT INTO replay_test_sets (name, description, prompts_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (name, description, json.dumps(prompts, ensure_ascii=False), now),
    )
    result = get_test_set(cur.lastrowid)  # type: ignore[return-value]
    if result is not None:
        result["item_count"] = len(prompts)
    return result  # type: ignore[return-value]


def delete_test_set(test_set_id: int) -> bool:
    """删除测试集。"""
    cur = db.execute("DELETE FROM replay_test_sets WHERE id=?", (test_set_id,))
    return cur.rowcount > 0


# ── COIG-Writer 玄幻子集导入 ────────────────────────────────


def import_coig_xianxia(name: str = "coig-xianxia-replay", max_items: int = 50) -> dict[str, Any]:
    """从 COIG-Writer 数据集导入玄幻（仙侠）子集作为回放测试集。

    依赖 datasets 库 + 联网。失败则抛出明确错误（调用方可降级用手动测试集）。

    COIG-Writer 结构：prompt / thought / output 三元组，含仙侠品类。
    这里只取 prompt（创作需求）作为回放的 request，output 不用（量级不匹配）。
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "导入 COIG-Writer 需要 datasets 库：pip install datasets"
        ) from exc

    logger.info("加载 COIG-Writer 数据集...")
    ds = load_dataset("m-a-p/COIG-Writer", split="train")

    # COIG-Writer 字段：prompt / thought / output（可能含 genre 标记）
    # 仙侠子集筛选：prompt 或 output 含仙侠/玄幻/修真等关键词
    xianxia_keywords = {"仙侠", "玄幻", "修真", "修仙", "炼气", "金丹", "元婴"}
    prompts: list[dict[str, str]] = []
    for row in ds:
        prompt_text = str(row.get("prompt", "")).strip()
        output_text = str(row.get("output", "")).strip()
        combined = prompt_text + output_text
        # 匹配玄幻/仙侠品类
        if not any(kw in combined for kw in xianxia_keywords):
            continue
        if not prompt_text:
            continue
        prompts.append({"request": prompt_text, "genre": "玄幻"})
        if len(prompts) >= max_items:
            break

    if not prompts:
        raise RuntimeError("COIG-Writer 未匹配到玄幻/仙侠样本（可能数据结构变化）")

    logger.info("COIG-Writer 匹配到 %d 条玄幻样本", len(prompts))
    return create_test_set(
        name=name, prompts=prompts,
        description=f"COIG-Writer 玄幻/仙侠子集，{len(prompts)} 条创作需求",
    )


# ── 默认测试集（无 COIG-Writer 时的兜底）──


def ensure_default_test_set() -> dict[str, Any]:
    """确保存在默认玄幻回放测试集（无则用内置样本创建）。

    内置样本是几个典型玄幻创作需求（金手指/升级/打脸套路场景），
    供 A/B 回放在没有 COIG-Writer 时也能跑。
    """
    existing = db.query_one("SELECT id FROM replay_test_sets WHERE name='default-xianxia'")
    if existing:
        return get_test_set(existing["id"])  # type: ignore[return-value]

    default_prompts = [
        {
            "request": "写一部玄幻小说：主角获得上古传承金手指，从废柴逆袭，打脸曾经欺辱他的家族。要爽点密集，升级流。",
            "genre": "玄幻",
        },
        {
            "request": "修仙题材：凡人少年偶得仙缘，踏上修真之路。强调境界突破的爽感和宗门斗争。",
            "genre": "玄幻",
        },
        {
            "request": "玄幻系统文：主角绑定升级系统，完成任务获奖励。节奏要快，每章有爽点钩子。",
            "genre": "玄幻",
        },
    ]
    return create_test_set(
        name="default-xianxia", prompts=default_prompts,
        description="内置默认玄幻回放测试集（3 条典型套路场景）",
    )


# ── 多文风多题材测试集（Phase 0 T0.2，D21 + S3）──
# 目的：A/B 验证 + D10 契约测试的输入。关键不是「量」，是「覆盖差异化的文风/
# 节奏/题材」，这样才能暴露进化出的 middleware「改坏文艺向/慢节奏」这类误伤
# （测试集分布外的低概率高危，D10 要防的）。
#
# 4 类 × 3 条 = 12 条：
#   - 爽文（爽点密集/升级/打脸）：进化正目标，应高分
#   - 文艺向（重情感/意境/慢热，反对爽点密集）：误伤高发区，应保住
#   - 慢节奏（铺垫厚重/世界观优先/不赶进度）：误伤高发区，应保住
#   - 都市/现实（非玄幻，验证跨题材）：泛化验证


_MULTISTYLE_PROMPTS = [
    # ── 爽文类（进化正目标）──
    {
        "request": "写一部玄幻爽文：主角穿越成废柴少爷，觉醒上古血脉金手指，一路打脸家族敌人。要求爽点密集，每章有反转或打脸，升级节奏快。",
        "genre": "玄幻-爽文",
        "style_profile": "dense_payoff",
    },
    {
        "request": "系统流玄幻：主角绑定签到系统，每日签到获神级奖励。强调装逼打脸和逆袭快感，章末留追读钩子。",
        "genre": "玄幻-爽文",
        "style_profile": "dense_payoff",
    },
    {
        "request": "无敌流修仙：主角开局即无敌，重点写各方势力的震惊和主角的从容装逼。爽点在于碾压和反差。",
        "genre": "玄幻-爽文",
        "style_profile": "dense_payoff",
    },
    # ── 文艺向（误伤高发区：进化出的『加快节奏』middleware 会毁掉这类）──
    {
        "request": "写一部文艺向修仙：重点不是爽点升级，而是主角对大道、生死、情义的感悟。文字要有意境和留白，情感细腻，反对快节奏爽点密集。",
        "genre": "玄幻-文艺",
        "style_profile": "literary",
    },
    {
        "request": "一部偏文学性的仙侠：讲述一个老修士暮年回首一生的故事，节奏舒缓，重在人物内心的苍凉与释然。不要打脸升级套路。",
        "genre": "玄幻-文艺",
        "style_profile": "literary",
    },
    {
        "request": "写修真界的爱情：两个道侣跨越百年的情感纠葛，重在情感的克制、错过与重逢。文风要含蓄隽永，不要爽文式直白。",
        "genre": "玄幻-文艺",
        "style_profile": "literary",
    },
    # ── 慢节奏（误伤高发区：进化出的『每章必爽点』middleware 会毁掉这类）──
    {
        "request": "写一部慢节奏史诗玄幻：开篇用大量篇幅铺垫世界观、势力格局和凡人生活，前几章可以无爽点，重在氛围和厚重感。故事在第二卷才进入高潮。",
        "genre": "玄幻-慢热",
        "style_profile": "slow_burn",
    },
    {
        "request": "凡人流修仙：主角资质平庸，修炼缓慢，重点写修炼的艰辛、资源的匮乏和一步一个脚印的成长。反对天才流和快速升级。",
        "genre": "玄幻-慢热",
        "style_profile": "slow_burn",
    },
    {
        "request": "一部重视设定的修仙：花大量笔墨构建修炼体系的严密逻辑、丹药品阶、阵法原理。剧情为设定服务，可以慢，但要自洽有深度。",
        "genre": "玄幻-慢热",
        "style_profile": "slow_burn",
    },
    # ── 都市/现实（跨题材泛化验证）──
    {
        "request": "写一部都市职场小说：主角是互联网公司中层，面临裁员危机和职场政治。重在写实的人物博弈和生活质感，不要任何超自然元素。",
        "genre": "都市",
        "style_profile": "realistic",
    },
    {
        "request": "悬疑推理：一起密室杀人案，主角是退休刑警。重在逻辑推理、线索铺垫和反转，文风冷硬克制。",
        "genre": "悬疑",
        "style_profile": "realistic",
    },
    {
        "request": "现实题材家庭小说：讲述一个普通家庭三代人的故事，重在生活细节、代际冲突和情感羁绊。朴实无华，不要戏剧化冲突。",
        "genre": "现实",
        "style_profile": "realistic",
    },
]


def ensure_default_multistyle_test_set() -> dict[str, Any]:
    """确保存在多文风多题材默认测试集（D21 + S3）。

    4 类（爽文/文艺/慢热/现实）× 3 条 = 12 条。
    关键作用：覆盖 A/B 验证的差异化场景，让 D10 契约测试能拦住
    『进化出的 middleware 改坏文艺向/慢节奏』这类测试集分布外的误伤。
    """
    existing = db.query_one("SELECT id FROM replay_test_sets WHERE name='default-multistyle'")
    if existing:
        return get_test_set(existing["id"])  # type: ignore[return-value]

    return create_test_set(
        name="default-multistyle", prompts=_MULTISTYLE_PROMPTS,
        description=(
            "多文风多题材默认测试集（12 条）：爽文/文艺/慢热/现实 各 3 条。"
            "用于 A/B 验证 + D10 契约测试，覆盖『改坏文艺向/慢节奏』等误伤场景。"
        ),
    )
