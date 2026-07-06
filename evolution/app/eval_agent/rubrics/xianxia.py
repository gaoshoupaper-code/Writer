"""玄幻/修仙网文评估 rubric。

双层维度（需求决策：双层并行各打分）：
  - 内容维度 8 项：评"作品好不好看"（玄幻网文商业标准）
  - subagent 维度 4 项：评"各环节能力强弱"（定位进化点）

连续分制（0.0~1.0），每维度给出 low/mid/high 三档锚点描述作为打分参考。
judge 在 0.0~1.0 区间连续打分（可取小数如 0.7），三档锚点用于校准判断。

设计依据：
  - 需求文档维度草案 + Fiction_Eval annotation methodology
  - 网文领域公开标准：
    · 学术量化四要素（环境/情节/人物/主线，百度百科"网络小说量化评分标准"）
    · 编辑审稿维度（开篇即时吸引力/故事持续期待感）
    · 17K 官方创作指南（爽点频率：每章 1-2 个）
    · 番茄官方构思五步（题材→梗概→人设→大纲→开篇三章）
  - eval 聚焦全局系统性维度，不重复执行端 review 的逐章细粒度审查
"""

from __future__ import annotations

# ── 内容维度：玄幻网文商业标准 ──────────────────────────────
# 评的是 writing subagent 的正文交付物（作品本体）。
# 8 个维度：爽点密度/节奏控制/章末钩子技法/套路契合/高开低走检测/人物讨喜度/设定呈现/全局AI味一致性。
# 维度判据来源：网文领域公开标准（见文件头注释）。

CONTENT_DIMENSIONS = [
    {
        "key": "爽点密度",
        "question": "单位篇幅内'爽'的频次是否足够？（打脸/升级/获宝/装逼/逆袭）参考基线：每章 1-2 个爽点（17K 创作指南）",
        "anchors": {
            "low": "连续多章无爽点，或全是铺垫流水账，读者无爽感",
            "mid": "有爽点但稀疏/分布不均，或爽点力度不足",
            "high": "爽点密集且分布均匀，几乎每章都有可感知的爽点递进",
        },
    },
    {
        "key": "节奏控制",
        "question": "情节推进快慢、张弛是否得当？（黄金三章理论：节奏压强与信息冰山）",
        "anchors": {
            "low": "严重拖沓（大段无意义描写）或严重赶工（关键情节一笔带过）",
            "mid": "节奏基本可读但有局部拖沓/赶工",
            "high": "张弛有度，铺垫与高潮交替自然，无注水段落",
        },
    },
    {
        "key": "章末钩子技法",
        "question": "章末/情节转折处的悬念钩子技法是否到位？（注：评的是钩子技法到不到位，留存率是读者行为数据，LLM 无法判断）",
        "anchors": {
            "low": "章末平淡收束，无悬念，读者无追读动力",
            "mid": "部分章节有钩子但不一致或钩子吸引力弱",
            "high": "章末普遍设有有效钩子（悬念/反转/期待），追读动力强",
        },
    },
    {
        "key": "套路契合",
        "question": "玄幻核心套路（金手指/升级/打脸循环）是否到位？",
        "anchors": {
            "low": "套路错位（如金手指失效/无升级体系/类型特征缺失），不像玄幻",
            "mid": "有套路元素但运用生硬或套路不完整",
            "high": "玄幻核心套路（金手指/升级/打脸）运用成熟、循环自洽",
        },
    },
    {
        "key": "高开低走检测",
        "question": "前后质量是否一致？参照黄金 1-3 万字（编辑审稿维度：开篇即时吸引力 vs 后段持续期待感）。LLM 写小说常见病灶：前段精彩后段崩",
        "anchors": {
            "low": "明显高开低走，前 1/3 精彩后段严重注水/崩坏/烂尾",
            "mid": "前后有落差但不至于崩坏",
            "high": "前后质量稳定，无注水/崩坏（或后段更精彩）",
        },
    },
    {
        "key": "人物讨喜度",
        "question": "主角是否爽文型（果断/不圣母/有魅力），配角是否有辨识度？",
        "anchors": {
            "low": "主角窝囊/圣母/无魅力，配角脸谱化/无辨识度",
            "mid": "主角尚可但不够爽，或配角辨识度不足",
            "high": "主角符合爽文审美（果断/有魅力），配角各有特色",
        },
    },
    {
        "key": "设定呈现",
        "question": "正文是否将世界观/体系/金手指自然融入叙事，读者能否通过剧情理解设定运转？（纯读者视角：judge 只看正文，不对照设定文件，评的是呈现而非设定本身）",
        "anchors": {
            "low": "正文堆砌设定说明文，读者看不懂/没耐心看；世界观体系与叙事割裂，设定以背景资料形式罗列而非通过剧情展开",
            "mid": "设定基本融入叙事，但偶有说明文段落；或体系/金手指运转逻辑不够清晰，读者需自行脑补",
            "high": "设定自然融入叙事，读者通过剧情推进自然理解世界观/体系/金手指运转，无说明文堆砌",
        },
    },
    {
        "key": "全局AI味一致性",
        "question": "整部作品的 AI 写作痕迹是系统性问题还是偶发现象？（判系统性 vs 偶发，不逐章查）",
        "anchors": {
            "low": "AI 味系统性泛滥，多条痕迹在多章普遍出现，属系统性写作习惯",
            "mid": "有 AI 味痕迹但属偶发，集中在个别段落/个别章节，非系统性",
            "high": "整体无明显 AI 味，文字有自然的人味变化",
        },
    },
]

# AI 味检测锚点清单（复用执行端 writing_review 的 12 条 Anti-AI 检测，保持两端标准统一）
AI_ANTI_PATTERNS = [
    "完整闭环——段落走完'起因→经过→结果→感悟'",
    "副词堆砌——'缓缓/淡淡/微微/轻轻'高频修饰动作",
    "千人一面——不同角色使用相同反应模板（如都'瞳孔微缩''心中一凛'）",
    "辩论式对话——角色完整理性表达观点，A→B→A→B 严格交替",
    "情绪贴标签——'他感到X'而非用生理反应+微动作展示",
    "信息均匀分布——每段信息量和长度相近，缺乏疏密对比",
    "安全着陆——章末所有冲突都被解决，没有悬念余味",
    "展示后解释——动作展示后紧跟解释句，不信任读者理解力",
    "说明书式对话——对话中完整解释背景和逻辑",
    "句式同构——连续多句'主语+谓语+宾语'结构，缺乏变化",
    "四字套语堆叠——成语/套语连续出现或 500 字内超过 3 个",
    "情绪三连——'他感到X，同时有些Y，内心深处还有一丝Z'式表达",
]

# ── subagent 维度：各环节能力 ────────────────────────────────
# 4 个 subagent 各评一个核心能力维度，定位"哪个环节产出差"。
# 每个 subagent 评它自己的交付物（见 eval_extractor）。

SUBAGENT_DIMENSIONS = [
    {
        "agent": "interview",
        "key": "需求理解准确度",
        "question": "demand.md 是否准确抓准了用户想写的玄幻类型/金手指/爽点偏好？",
        "anchors": {
            "low": "需求理解严重偏差（类型/金手指/爽点方向都错了）或核心维度缺失",
            "mid": "抓到大方向但有细节遗漏或理解不够精准",
            "high": "精准抓准用户意图，核心/设定/风格/约束四层维度齐全且贴合",
        },
    },
    {
        "agent": "storybuilding",
        "key": "设定自洽与新颖度",
        "question": "世界观/修炼体系/金手指设定是否自洽且有一定新颖度？（注：评设定本身，与内容层'设定呈现'不重复——本维度评设定设计质量，内容层评正文呈现效果）",
        "anchors": {
            "low": "设定矛盾不自洽，或完全是烂大街套路无新意",
            "mid": "基本自洽但新颖度不足，或有小处矛盾",
            "high": "设定自洽完整，金手指/体系有一定特色不落俗",
        },
    },
    {
        "agent": "detail-outline",
        "key": "爽点排布与结构",
        "question": "大纲的爽点排布、节奏结构、伏笔回收规划是否合理？",
        "anchors": {
            "low": "爽点排布混乱/无节奏结构/伏笔不回收",
            "mid": "有结构但爽点排布/节奏有明显瑕疵",
            "high": "爽点排布合理、节奏结构清晰、伏笔有回收规划",
        },
    },
    {
        "agent": "writing",
        "key": "爽点演绎能力",
        "question": "正文的文字表现力、画面感、爽点的'演绎'能力如何？（爽点写不写得出来）",
        "anchors": {
            "low": "文字干瘪/无画面感/爽点写不出来（大纲有爽点正文实现不了）",
            "mid": "能写但表现力一般，爽点演绎力度不足",
            "high": "文字有画面感有张力，爽点演绎到位有感染力",
        },
    },
]

# badcase 阈值（D9：默认值 + 后期调参）
# 任一维度低于此阈值即标记该维度为 badcase（需求决策：任一维度低即 badcase）。
# 注：连续分制（0.0~1.0）下阈值为绝对值语义，当前数值为初始保守值，待跑数据校准。
CONTENT_BADCASE_THRESHOLD = 0.6
SUBAGENT_BADCASE_THRESHOLD = 0.5


def build_content_rubric_prompt() -> str:
    """构造内容维度 judge 的 rubric prompt 片段。"""
    lines = [
        "你是玄幻/修仙网文的质量评估员。请从**商业网文标准**评估下面这部作品的正文。",
        "按以下 8 个维度逐一打分，每个维度在 0.0~1.0 区间连续打分（可取小数如 0.7），low/mid/high 三档为校准锚点参考：\n",
    ]
    for dim in CONTENT_DIMENSIONS:
        lines.append(f"### {dim['key']}")
        lines.append(f"评估问题：{dim['question']}")
        lines.append(f"- low（~0.0）：{dim['anchors']['low']}")
        lines.append(f"- mid（~0.5）：{dim['anchors']['mid']}")
        lines.append(f"- high（~1.0）：{dim['anchors']['high']}")
        lines.append("")
    # 第8维的 AI 味锚点清单单独注入
    lines.append("### AI 味检测锚点清单（用于「全局AI味一致性」维度）")
    lines.append("逐条对照以下痕迹判断 AI 味是系统性泛滥还是偶发：")
    for i, p in enumerate(AI_ANTI_PATTERNS, 1):
        lines.append(f"{i}. {p}")
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
        f"- low（~0.0）：{dim['anchors']['low']}",
        f"- mid（~0.5）：{dim['anchors']['mid']}",
        f"- high（~1.0）：{dim['anchors']['high']}",
        "",
        "判定：连续分制 0.0~1.0，>= 0.7 为 pass，0.4~0.7 为 review，< 0.4 为 fail。",
    ]
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
所有分值在 0.0~1.0 连续区间，可取小数（如 0.7）。
evidence 必须包含每个维度的依据。"""


def content_dim_keys() -> list[str]:
    return [d["key"] for d in CONTENT_DIMENSIONS]


def subagent_dim_keys() -> list[str]:
    return [d["agent"] for d in SUBAGENT_DIMENSIONS]
