"use client";

/**
 * 单个 Agent 的要素卡片。
 *
 * 根据 tab 渲染不同要素：
 * - prompt：Markdown 渲染 prompt 全文
 * - skills：每个 skill 一个折叠块，展开看 SKILL.md 全文
 * - middleware：middleware 元信息列表，源码懒加载折叠
 */
import type { AgentElements } from "@/lib/harness-types";
import { Markdown } from "@/components/Markdown";
import { MiddlewareSource } from "./MiddlewareSource";
import type { ElementTab } from "./ElementTabs";

interface Props {
  agent: AgentElements;
  tab: ElementTab;
  version: number;
  hasSource: boolean;
}

// subagent 机器名 → 中文角色名（与后端 _SUBAGENT_ROLE_MAP 对齐，展示用）
const AGENT_LABEL: Record<string, string> = {
  meta: "Meta（总控）",
  interview: "需求访谈",
  storybuilding: "故事构建",
  detail_outline: "细纲生成",
  writing: "正文写作",
  general_purpose: "通用助手",
};

export function AgentCard({ agent, tab, version, hasSource }: Props) {
  const label = AGENT_LABEL[agent.name] ?? agent.name;

  return (
    <div className="harness-agent-card">
      <div className="harness-agent-card-head">
        <span className="harness-agent-name">{label}</span>
        <span className="harness-agent-kind mono text-mute">
          {agent.kind}
        </span>
      </div>

      <div className="harness-agent-card-body">
        {tab === "prompt" && <PromptBody agent={agent} />}
        {tab === "skills" && <SkillsBody agent={agent} />}
        {tab === "middleware" && (
          <MiddlewareBody agent={agent} version={version} hasSource={hasSource} />
        )}
      </div>
    </div>
  );
}

/** Prompt Tab：Markdown 渲染全文。 */
function PromptBody({ agent }: { agent: AgentElements }) {
  const body = agent.prompt.body;
  if (!body) {
    return <p className="text-dim" style={{ fontSize: 13 }}>该 Agent 无 prompt。</p>;
  }
  return <Markdown>{body}</Markdown>;
}

/** Skills Tab：每个 skill 一个折叠块，展开看 SKILL.md 全文。 */
function SkillsBody({ agent }: { agent: AgentElements }) {
  if (agent.skills.length === 0) {
    return <p className="text-dim" style={{ fontSize: 13 }}>该 Agent 无 skill。</p>;
  }
  return (
    <div className="harness-skills-list">
      {agent.skills.map((skill) => (
        <details key={skill.path} className="harness-skill-block">
          <summary className="harness-skill-summary">
            <span>{skill.name}</span>
            <span className="mono text-mute" style={{ fontSize: 11 }}>
              {skill.path}
            </span>
          </summary>
          <div className="harness-skill-content">
            {skill.content ? (
              <Markdown>{skill.content}</Markdown>
            ) : (
              <p className="text-dim" style={{ fontSize: 13 }}>
                无法读取：{skill.load_error || "未知原因"}
              </p>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}

/** Middleware Tab：middleware 元信息列表，源码懒加载折叠。 */
function MiddlewareBody({
  agent,
  version,
  hasSource,
}: {
  agent: AgentElements;
  version: number;
  hasSource: boolean;
}) {
  if (agent.middlewares.length === 0) {
    return <p className="text-dim" style={{ fontSize: 13 }}>该 Agent 无 middleware。</p>;
  }
  return (
    <div className="harness-mw-list">
      {agent.middlewares.map((mw, i) => (
        <MiddlewareSource
          key={`${mw.class_name}-${mw.hook}-${mw.group}-${i}`}
          mw={mw}
          version={version}
          hasSource={hasSource}
        />
      ))}
    </div>
  );
}
