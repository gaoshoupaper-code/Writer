import { forwardRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { EvolveMessage, EvolvePoint } from "@/lib/api";
import ProposalCard from "./ProposalCard";

/**
 * 单条对话消息渲染（决策 Y/X）。
 *
 * 用户消息：纯文本气泡（决策 X，无 markdown 渲染避免输入框输入的 # 被误解为标题）。
 * Agent 消息：markdown 渲染（标题/列表/表格/代码块）+ 内嵌进化点卡片。
 * 系统消息：系统事件（落地进度等，决策 W）。
 *
 * 进化点卡片渲染逻辑：
 *   - 通过 related_points 字段关联进化点 id 列表
 *   - Agent 在 markdown 里用 `[[propose:EP-2]]` 语法标记插入位置（决策 Y）
 *   - 本组件解析 markdown，遇到 [[propose:<id>]] 占位 → 渲染对应 ProposalCard
 *
 * 双向高亮联动（决策 N）：data-message-id 供浮窗点击时滚动定位。
 */
interface Props {
  message: EvolveMessage;
  points: EvolvePoint[]; // 当前 session 全部进化点（供 markdown 占位符渲染卡片）
  highlightedPointId?: string | null; // 当前高亮的进化点 id（来自浮窗点击）
  onPointClick?: (pointId: string) => void; // 点击卡片 → 滚动到该点讨论位置
}

// 解析 markdown 里的 [[propose:<id>]] 占位符，把内容拆为「文本段 + 进化点」序列
function parseEmbeddedPoints(
  content: string,
  points: EvolvePoint[],
): Array<{ type: "text"; text: string } | { type: "point"; point: EvolvePoint }> {
  const result: Array<{ type: "text"; text: string } | { type: "point"; point: EvolvePoint }> = [];
  const pointsById = new Map(points.map((p) => [p.id, p]));

  // 匹配 [[propose:<id>]] 或 [[propose:EP-<seq>]]
  const regex = /\[\[propose:([a-f0-9]{6,32}|EP-\d+)\]\]/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(content)) !== null) {
    const before = content.slice(lastIdx, match.index);
    if (before) result.push({ type: "text", text: before });

    const idOrEp = match[1];
    let point: EvolvePoint | undefined;
    if (idOrEp.startsWith("EP-")) {
      // EP-<seq> 形式：按 seq 查
      const seq = parseInt(idOrEp.slice(3), 10);
      point = points.find((p) => p.seq === seq);
    } else {
      point = pointsById.get(idOrEp);
    }

    if (point) {
      result.push({ type: "point", point });
    } else {
      // 找不到对应进化点，原样保留占位符（不丢信息）
      result.push({ type: "text", text: match[0] });
    }
    lastIdx = match.index + match[0].length;
  }
  const tail = content.slice(lastIdx);
  if (tail) result.push({ type: "text", text: tail });
  return result;
}

const EvolveMessageBubble = forwardRef<HTMLDivElement, Props>(function EvolveMessageBubble(
  { message, points, highlightedPointId, onPointClick },
  ref,
) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system" || message.role === "tool";

  if (isSystem) {
    // 系统消息：单行小字（落地进度事件等，决策 W）
    return (
      <div ref={ref} className="evolve-msg system" data-message-id={message.id}>
        <span className="system-content">{message.content}</span>
      </div>
    );
  }

  // 用户消息：纯文本气泡（不渲染 markdown，避免输入 # 被误解）
  if (isUser) {
    return (
      <div ref={ref} className="evolve-msg user" data-message-id={message.id}>
        <div className="msg-bubble">
          <p className="msg-text">{message.content}</p>
        </div>
      </div>
    );
  }

  // Agent 消息：markdown + 内嵌进化点卡片
  const segments = parseEmbeddedPoints(message.content, points);

  return (
    <div ref={ref} className="evolve-msg assistant" data-message-id={message.id}>
      <div className="msg-avatar" aria-hidden>
        <span className="avatar-glyph">🧬</span>
      </div>
      <div className="msg-body">
        {segments.map((seg, idx) => {
          if (seg.type === "text") {
            return (
              <div key={idx} className="msg-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{seg.text}</ReactMarkdown>
              </div>
            );
          }
          return (
            <ProposalCard
              key={idx}
              point={seg.point}
              highlighted={highlightedPointId === seg.point.id}
              {...(onPointClick ? { onPointClick: () => onPointClick(seg.point.id) } : {})}
            />
          );
        })}
      </div>
    </div>
  );
});

export default EvolveMessageBubble;
