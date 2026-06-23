"""文生图能力域（Phase 3，DD1-DD21 需求落地）。

闭环（D4/D5/D6）：用户需求 → Agent 优化 3 版提示词 → 每版双采样生 2 图 →
Agent 视觉自评（D5 第一层）→ HITL interrupt 等用户打分（D5 第二层）→
迭代或收尾 → 问是否持久化成 Skill（D8）。

子模块：
- ``agent``：ImageAgentService（继承 platform BaseAgentService）
- ``tools``：generate_images / analyze_image / persist_skill 工具
- ``providers/bytedance``：字节生图/视觉 API（D3/D14 占位）
- ``prompts``：image-agent 系统提示词
- ``store``：ImageRepository（images 表 CRUD）
"""
