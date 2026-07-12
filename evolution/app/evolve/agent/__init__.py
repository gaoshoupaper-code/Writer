"""进化 Agent（单体）—— Writer 项目的 Agent 架构进化专家。

重构后的单体进化 Agent，替代原 driver + plan/execute 三体结构（决策 S1）。
能看懂 harness 包全部要素 + DeepAgent 框架运转机理，通过专用工具集
安全地改 prompts/middleware/tool/subagents/skills 五个要素。

模块：
  agent.py          build_evolve_agent() 单体装配
  prompt.py         7 段全景 system prompt
  tools/            15 工具集（inspect 探查 / writers 写 / flow 流程）
  middleware/       FlowGuardMiddleware 轻量产出约束

设计依据：.claude/md/20260712_160000_重构进化端两个Agent设计.md
"""
