/**
 * Harness（Agent 要素）页共享常量。
 *
 * hook 顺序按 DeepAgents AgentMiddleware 生命周期（langchain.agents.middleware.types），
 * 列名直接用英文 hook 标识符，贴合 DeepAgents 原生术语——
 * 后端 VALID_HOOKS 是 frozenset（无序），前端必须用这里的 HOOK_ORDER 排列泳道图列。
 * agent 中文名与后端 elements_api._SUBAGENT_ROLE_MAP 对齐，meta_pipeline 是前端补充。
 */

/** 6 个生命周期 hook 的执行顺序（before_agent → after_agent），即 DeepAgents AgentMiddleware 的 6 个可覆盖方法 */
export const HOOK_ORDER = [
  "before_agent",
  "before_model",
  "wrap_model_call",
  "after_model",
  "wrap_tool_call",
  "after_agent",
] as const;

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
