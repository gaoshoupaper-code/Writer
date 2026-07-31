"""LLM-judge 的内置默认 rubric（好坏标准）。

需求阶段约定"用户预设明确标准"，但该标准文档尚未产出。
此处提供内置默认 rubric 让系统先跑起来，开发者可在后续于界面调整。
rubric 是文本形式的评估维度 + 合格判定，会拼进 LLM prompt。
"""

from __future__ import annotations

# 默认评估维度。每个维度：维度名 + 关注点 + 不合格信号。
DEFAULT_RUBRIC = """你是写作 Agent 系统的质量评估员。请按以下维度评估一次 agent 执行：

## 评估维度（每项 0~1 分，1=完全达标，0=严重不达标）

1. **需求覆盖**：最终产出是否回应了用户原始创作需求。
   - 不合格信号：产出与需求无关、关键需求点缺失。

2. **内容完整度**：产出是否结构完整（如大纲有起承转合、角色有完整设定）。
   - 不合格信号：内容残缺、结构断裂、明显未完成。

3. **逻辑一致性**：产出内部是否自洽（人物动机、情节推进、世界观）。
   - 不合格信号：前后矛盾、逻辑跳跃、人物行为不合理。

4. **执行过程合理性**：agent 编排是否高效（有无冗余/反复/卡死）。
   - 不合格信号：同一动作重复多次、明显空转、长耗时无进展。

## 判定
- 综合分 >= 0.7 → verdict: pass
- 0.4 ~ 0.7 → verdict: review（需人工关注）
- < 0.4 → verdict: fail

## 可量化异常模式提取
若发现可量化的问题（能用数值阈值表达的），按以下指标之一提炼：
- duration_ms（总耗时毫秒）
- total_tokens（token 消耗）
- error_count（error 节点数）
- event_count（事件总数）
例如：若观察到 trace 反复重试导致 event_count 异常高，可提炼 "event_count > 800"。
若异常无法量化（如"逻辑乱"），rule_suggestions 留空，只在评分里体现。
"""

# LLM 必须返回的 JSON schema（prompt 里明确要求此格式）
OUTPUT_FORMAT = """\n请严格按以下 JSON 格式返回（不要任何额外文字、不要 markdown 代码块）：
{
  "scores": {"需求覆盖": 0.0~1.0, "内容完整度": 0.0~1.0, "逻辑一致性": 0.0~1.0, "执行过程合理性": 0.0~1.0},
  "overall": 0.0~1.0,
  "verdict": "pass" | "review" | "fail",
  "summary": "一句话总结这次执行的质量与主要问题",
  "rule_suggestions": [
    {"metric": "duration_ms|total_tokens|error_count|event_count", "op": ">|>=|<|<=|==|!=", "threshold": "数值字符串", "reason": "为什么这个阈值能捕捉此问题"}
  ]
}
rule_suggestions 可为空数组。"""

# judge_rubric 在 prompts 表里的 name（配置化后改评估标准改这条 prompt 即可）。
JUDGE_RUBRIC_PROMPT_NAME = "judge_rubric"


def get_rubric() -> str:
    """获取当前生效的 rubric 文本（评估维度 + 输出格式）。

    优先从 prompts 表的 judge_rubric（production 版本）拉，拉取失败（表为空 /
    DB 未初始化 / 任何异常）则降级用本文件内置的 DEFAULT_RUBRIC + OUTPUT_FORMAT。

    这样改评估标准只需改 prompt 版本内容，无需改代码；同时内置兜底保证
    系统在任何情况下都能跑起来。
    """
    try:
        from app.improvement.prompts_repo import get_prompt_content

        result = get_prompt_content(JUDGE_RUBRIC_PROMPT_NAME, "production")
        if result and result.get("content"):
            return result["content"]
    except Exception:
        # 任何异常（DB 未就绪、表不存在、import 失败）都降级到内置 rubric。
        # judge 是派生功能，不应因 rubric 取不到而崩溃。
        pass
    return DEFAULT_RUBRIC + OUTPUT_FORMAT
