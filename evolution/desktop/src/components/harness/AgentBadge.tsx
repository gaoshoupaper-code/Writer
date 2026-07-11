import { agentLabel } from "@/lib/harness-constants";

/**
 * Agent 归属徽章：把 agent 机器名转成中文角色名显示。
 *
 * 在按要素类型分组的 Tab 里，每条要素旁标这个徽章，
 * 让用户知道它属于哪个 Agent（主控 / 故事构建 / …）。
 */
export function AgentBadge({ name }: { name: string }) {
  return <span className="agent-badge">{agentLabel(name)}</span>;
}
