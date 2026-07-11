/**
 * Harness（Agent 要素）页共享常量。
 *
 * hook 顺序按 DeepAgents/LangGraph middleware 生命周期，不是字母序——
 * 后端 VALID_HOOKS 是 frozenset（无序），前端必须用这里的 HOOK_ORDER 排列泳道图列。
 * agent 中文名与后端 elements_api._SUBAGENT_ROLE_MAP 对齐，meta_pipeline 是前端补充。
 */

/** 6 个生命周期 hook 的执行顺序（before_agent → after_agent） */
export const HOOK_ORDER = [
  "before_agent",
  "before_model",
  "wrap_model_call",
  "after_model",
  "wrap_tool_call",
  "after_agent",
] as const;

/** hook → 中文标签 + 阶段说明 */
export const HOOK_LABELS: Record<string, { name: string; desc: string }> = {
  before_agent: { name: "Agent 启动前", desc: "Agent 开始执行前" },
  before_model: { name: "模型调用前", desc: "每次调 LLM 前" },
  wrap_model_call: { name: "包裹模型调用", desc: "包裹整个模型调用过程" },
  after_model: { name: "模型输出后", desc: "LLM 输出后、返回前" },
  wrap_tool_call: { name: "包裹工具调用", desc: "包裹工具执行" },
  after_agent: { name: "Agent 结束后", desc: "Agent 执行完毕后" },
};

/** agent 机器名 → 中文角色名（对齐后端 _SUBAGENT_ROLE_MAP，meta_pipeline 前端补充为"主控"） */
export const AGENT_LABELS: Record<string, string> = {
  meta_pipeline: "主控",
  interview: "需求访谈",
  storybuilding: "故事构建",
  detail_outline: "细纲生成",
  writing: "正文写作",
  general_purpose: "通用助手",
};

/** 获取 agent 中文名，未知 agent 回退机器名 */
export function agentLabel(name: string): string {
  return AGENT_LABELS[name] ?? name;
}
