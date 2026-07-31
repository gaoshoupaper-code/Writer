"""玄幻/修仙网文评估 rubric（T1.2，决策 D11 粗标三档）。

双层维度（需求决策：双层并行各打分）：
  - 内容维度 6 项：评"作品好不好看"（玄幻网文商业标准）
  - subagent 维度 4 项：评"各环节能力强弱"（定位进化点）

粗标三档（0/0.5/1），每维度给出三档对应的判定信号。
这是 Phase 1 验证用，分数符合直觉后再精修（D11）。

设计依据：需求文档维度草案 + Fiction_Eval annotation methodology。
"""

from __future__ import annotations

# ── 内容维度：玄幻网文商业标准 ──────────────────────────────
# 评的是 writing subagent 的正文交付物（作品本体）。
# 6 个维度对应需求草案：爽点密度/节奏/留存钩子/套路契合/高开低走/人物讨喜。

CONTENT_DIMENSIONS = [
    {
        "key": "爽点密度",
        "question": "单位篇幅内'爽'的频次是否足够？（打脸/升级/获宝/装逼/逆袭）",
        "levels": {
            0.0: "连续多章无爽点，或全是铺垫流水账，读者无爽感",
            0.5: "有爽点但稀疏/分布不均，或爽点力度不足",
            1.0: "爽点密集且分布均匀，几乎每章都有可感知的爽点递进",
        },
    },
    {
        "key": "节奏控制",
        "question": "情节推进快慢、张弛是否得当？",
        "levels": {
            0.0: "严重拖沓（大段无意义描写）或严重赶工（关键情节一笔带过）",
            0.5: "节奏基本可读但有局部拖沓/赶工",
            1.0: "张弛有度，铺垫与高潮交替自然，无注水段落",
        },
    },
    {
        "key": "留存钩子",
        "question": "章末/情节转折处的悬念钩子是否到位？（决定弃书率）",
        "levels": {
            0.0: "章末平淡收束，无悬念，读者无追读动力",
            0.5: "部分章节有钩子但不一致或钩子吸引力弱",
            1.0: "章末普遍设有有效钩子（悬念/反转/期待），追读动力强",
        },
    },
    {
        "key": "套路契合",
        "question": "玄幻核心套路（金手指/升级/打脸循环）是否到位？",
        "levels": {
            0.0: "套路错位（如金手指失效/无升级体系/类型特征缺失），不像玄幻",
            0.5: "有套路元素但运用生硬或套路不完整",
            1.0: "玄幻核心套路（金手指/升级/打脸）运用成熟、循环自洽",
        },
    },
    {
        "key": "高开低走检测",
        "question": "前后质量是否一致？（LLM 写小说常见病灶：前段精彩后段崩）",
        "levels": {
            0.0: "明显高开低走，前1/3精彩后段严重注水/崩坏/烂尾",
            0.5: "前后有落差但不至于崩坏",
            1.0: "前后质量稳定，无注水/崩坏（或后段更精彩）",
        },
    },
    {
        "key": "人物讨喜度",
        "question": "主角是否爽文型（果断/不圣母/有魅力），配角是否有辨识度？",
        "levels": {
            0.0: "主角窝囊/圣母/无魅力，配角脸谱化/无辨识度",
            0.5: "主角尚可但不够爽，或配角辨识度不足",
            1.0: "主角符合爽文审美（果断/有魅力），配角各有特色",
        },
    },
]

# ── subagent 维度：各环节能力 ────────────────────────────────
# 4 个 subagent 各评一个核心能力维度，定位"哪个环节产出差"。
# 每个 subagent 评它自己的交付物（见 eval_extractor）。

SUBAGENT_DIMENSIONS = [
    {
        "agent": "interview",
        "key": "需求理解准确度",
        "question": "demand.md 是否准确抓准了用户想写的玄幻类型/金手指/爽点偏好？",
        "levels": {
            0.0: "需求理解严重偏差（类型/金手指/爽点方向都错了）或核心维度缺失",
            0.5: "抓到大方向但有细节遗漏或理解不够精准",
            1.0: "精准抓准用户意图，核心/设定/风格/约束四层维度齐全且贴合",
        },
    },
    {
        "agent": "storybuilding",
        "key": "设定自洽与新颖度",
        "question": "世界观/修炼体系/金手指设定是否自洽且有一定新颖度？",
        "levels": {
            0.0: "设定矛盾不自洽，或完全是烂大街套路无新意",
            0.5: "基本自洽但新颖度不足，或有小处矛盾",
            1.0: "设定自洽完整，金手指/体系有一定特色不落俗",
        },
    },
    {
        "agent": "detail-outline",
        "key": "爽点排布与结构",
        "question": "大纲的爽点排布、节奏结构、伏笔回收规划是否合理？",
        "levels": {
            0.0: "爽点排布混乱/无节奏结构/伏笔不回收",
            0.5: "有结构但爽点排布/节奏有明显瑕疵",
            1.0: "爽点排布合理、节奏结构清晰、伏笔有回收规划",
        },
    },
    {
        "agent": "writing",
        "key": "爽点演绎能力",
        "question": "正文的文字表现力、画面感、爽点的'演绎'能力如何？（爽点写不写得出来）",
        "levels": {
            0.0: "文字干瘪/无画面感/爽点写不出来（大纲有爽点正文实现不了）",
            0.5: "能写但表现力一般，爽点演绎力度不足",
            1.0: "文字有画面感有张力，爽点演绎到位有感染力",
        },
    },
]

# badcase 阈值（D9：默认值 + 后期调参）
# 任一维度低于此阈值即标记该维度为 badcase（需求决策：任一维度低即 badcase）。
CONTENT_BADCASE_THRESHOLD = 0.6
SUBAGENT_BADCASE_THRESHOLD = 0.5


def build_content_rubric_prompt() -> str:
    """构造内容维度 judge 的 rubric prompt 片段。"""
    lines = [
        "你是玄幻/修仙网文的质量评估员。请从**商业网文标准**评估下面这部作品的正文。",
        "按以下 6 个维度逐一打分（每项 0.0 / 0.5 / 1.0，可取中间值如 0.7）：\n",
    ]
    for dim in CONTENT_DIMENSIONS:
        lines.append(f"### {dim['key']}")
        lines.append(f"评估问题：{dim['question']}")
        for score, desc in sorted(dim["levels"].items()):
            lines.append(f"- {score}：{desc}")
        lines.append("")
    lines.append("判定：综合分 >= 0.7 为 pass，0.4~0.7 为 review，< 0.4 为 fail。")
    return "\n".join(lines)


def build_subagent_rubric_prompt(agent: str) -> str:
    """构造某 subagent 维度 judge 的 rubric prompt 片段。"""
    dim = next((d for d in SUBAGENT_DIMENSIONS if d["agent"] == agent), None)
    if dim is None:
        raise ValueError(f"无此 subagent 的 rubric: {agent}")
    lines = [
        f"你是玄幻/修仙网文系统的环节质量评估员。请评估 **{agent}** 环节的产出质量。",
        f"该环节的核心交付物见下方文本。\n",
        f"### 评估维度：{dim['key']}",
        f"评估问题：{dim['question']}",
    ]
    for score, desc in sorted(dim["levels"].items()):
        lines.append(f"- {score}：{desc}")
    lines.append("")
    lines.append("判定：1.0 为 pass，0.5 为 review，0.0 为 fail。")
    return "\n".join(lines)


def build_output_format(dim_keys: list[str]) -> str:
    """构造 judge 必须返回的 JSON 格式说明。"""
    keys_json = ", ".join(f'"{k}": 0.0~1.0' for k in dim_keys)
    return f"""
请严格按以下 JSON 格式返回（不要任何额外文字、不要 markdown 代码块）：
{{
  "scores": {{{keys_json}}},
  "overall": 0.0~1.0,
  "verdict": "pass" | "review" | "fail",
  "evidence": "{{每个维度一句话说明打分依据}}"
}}
evidence 必须包含每个维度的依据。"""


def content_dim_keys() -> list[str]:
    return [d["key"] for d in CONTENT_DIMENSIONS]


def subagent_dim_keys() -> list[str]:
    return [d["agent"] for d in SUBAGENT_DIMENSIONS]
