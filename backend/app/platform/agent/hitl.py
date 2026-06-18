"""HITL（Human-In-The-Loop）interrupt payload 统一协议（DD4）。

LangGraph 的 ``interrupt()`` 机制用于在 agent 执行中暂停等待人类反馈。本模块
定义统一的 payload 契约，让任意 domain 都能定义自己的 HITL 反馈类型，前端按
``kind`` 字段路由渲染。

协议设计（DD4）：
- payload 必须含 ``kind`` 字段，标识反馈类型，前端据此选择渲染组件。
- ``kind="choice"``：现有写作访谈的选项化反馈（向后兼容）。
- ``kind="image_review"``：文生图的图像评审反馈（3 版 × 双采样 + 打分）。
- 各 domain 可扩展自己的 ``kind`` 和对应 payload schema。

resume 值（用户反馈回传）同样按 ``kind`` 区分：
- ``choice``：字符串（现有行为，``InterviewOptions.buildResume()`` 拼接）。
- ``image_review``：结构化对象（含 3 版各 1-5 星 + 文本 + continue/stop）。

向后兼容：无 ``kind`` 字段的 payload 视为 ``choice``（现有访谈 payload 不含 kind）。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ── kind 类型 ──────────────────────────────────────────────

Kind = Literal["choice", "image_review"]
"""HITL 反馈类型。前端按此字段路由渲染组件。扩展新 domain 的 kind 时加入此 Literal。"""

# ── choice（现有写作访谈）payload ──────────────────────────


class AskUserOption(TypedDict, total=False):
    """访谈选项（与 writer/tools/ask_user.py 的 AskUserOption 对齐）。"""

    label: str
    description: str


class ChoiceInterruptPayload(TypedDict, total=False):
    """kind="choice" 的 interrupt payload（向后兼容现有访谈）。"""

    kind: Kind
    source: str
    question: str
    options: list[AskUserOption]
    multi_select: bool


# ── image_review（文生图评审）payload ─────────────────────


class ImageRef(TypedDict):
    """单张图片的引用（不含二进制，前端按 url 单独请求）。"""

    image_id: str
    url: str  # 指向 GET /api/images/{image_id}（DD8b）


class VersionImages(TypedDict, total=False):
    """一版的双采样图 + Agent 自评。"""

    version_id: str
    direction: str  # Agent 给这版的方向描述（D21）
    prompt: str  # 用的提示词
    images: list[ImageRef]  # 双采样 2 张
    agent_analysis: str  # D5 第一层自评结果（D14：质量+匹配度）


class ImageReviewInterruptPayload(TypedDict, total=False):
    """kind="image_review" 的 interrupt payload（每轮评审）。"""

    kind: Literal["image_review"]
    source: str  # 固定 "image-agent"
    round: int  # 第几轮
    versions: list[VersionImages]  # 3 版 × 双采样


# ── image_review 的 resume 值（用户反馈回传，结构化）──────


class VersionRating(TypedDict, total=False):
    """单版的用户评分 + 文本反馈（D13）。"""

    version_id: str
    score: int  # 1-5 星
    note: str  # 自由文本优化方向


class ImageReviewResume(TypedDict, total=False):
    """kind="image_review" 的 resume 值（结构化对象，非字符串）。

    前端 POST 此对象作为 ``resume``；后端 ``Command(resume=this)`` 恢复 agent，
    ask_user 工具返回值即此对象，agent 据此迭代或收尾。
    """

    kind: Literal["image_review"]
    round: int
    ratings: list[VersionRating]  # 3 版各一评
    overall_direction: str  # 用户给的整体优化方向（可选）
    action: Literal["continue", "stop"]  # 继续迭代 or 喊停（D6）


# ── 路由工具 ───────────────────────────────────────────────


def interrupt_kind(payload: Any) -> Kind:
    """从 interrupt payload 提取 kind，无 kind 字段时回退 "choice"（向后兼容）。

    现有写作访谈的 payload 不含 kind（``ask_user`` 工具直接构造 {question,options,...}），
    需兼容。新代码构造 payload 时应显式带 kind。
    """
    if isinstance(payload, dict):
        k = payload.get("kind")
        if k in ("choice", "image_review"):
            return k  # type: ignore[return-value]
    return "choice"


__all__ = [
    "Kind",
    "AskUserOption",
    "ChoiceInterruptPayload",
    "ImageRef",
    "VersionImages",
    "ImageReviewInterruptPayload",
    "VersionRating",
    "ImageReviewResume",
    "interrupt_kind",
]
