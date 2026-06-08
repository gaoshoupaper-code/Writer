import { useEffect, useMemo, useState } from "react";
import type { TraceContextSegment, TraceNode, TraceTodoItem } from "../../lib/types";
import { ChainIcon, SegmentIcon } from "./ChainNodeIcon";

/**
 * 链路视图右侧抽屉 — 展开节点详情。
 * - LLM: 展示关联 segments
 * - Todo: 展示任务列表
 * - Error: 展示完整错误栈
 * - 底部: 「查看全部上下文」按钮，展示全部 context segments
 */

type TraceChainDrawerProps = {
  node: TraceNode | null;
  context: TraceContextSegment[];
  todos: { anchor_id: string; items: TraceTodoItem[]; active_item?: string | null }[];
  inputMessages: unknown[] | null;
  /** 从 llm_end / tool_end 事件直接获取的节点输出（非 projector 重建） */
  nodeOutput: unknown[] | unknown | null;
  onClose: () => void;
};

/** segment kind → 标签 + 样式映射 */
const SEGMENT_KIND_CONFIG: Record<string, { label: string; className: string }> = {
  system: { label: "System", className: "system" },
  human: { label: "Human", className: "human" },
  ai: { label: "AI", className: "ai" },
  tool: { label: "Tool", className: "tool" },
  error: { label: "Error", className: "error" },
  todo: { label: "Todo", className: "todo" },
};

export function TraceChainDrawer({ node, context, todos, inputMessages, nodeOutput, onClose }: TraceChainDrawerProps) {
  const [drawerView, setDrawerView] = useState<"detail" | "all">("detail");

  // 节点切换时重置视图
  useEffect(() => {
    setDrawerView("detail");
  }, [node?.node_id]);

  // N4: 选中 subagent 时过滤掉 main agent 段落
  const filteredContext = useMemo(() => {
    if (!node || node.agent_role === "main") return context;
    return context.filter(seg => seg.agent_role !== "main");
  }, [context, node]);

  if (!node) return null;

  const canShowContext = node.kind === "llm" || node.kind === "tool";
  const eventNum = eventNumberLabel(node.raw_event_ids);

  return (
    <aside className="trace-chain-drawer">
      {/* 抽屉头部 */}
      <div className="drawer-header">
        {drawerView === "all" ? (
          <button className="drawer-back-btn" type="button" onClick={() => setDrawerView("detail")}>
            ← 返回节点详情
          </button>
        ) : (
          <>
            <span className={`chain-node-icon ${node.kind}`}>
              <ChainIcon kind={node.kind} size={16} />
            </span>
            <span className="drawer-node-label">{node.model_name || node.tool_name || node.label}</span>
            {eventNum ? <span className="drawer-event-num">{eventNum}</span> : null}
          </>
        )}
        <button className="drawer-close-btn" type="button" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      {/* 抽屉内容区 */}
      <div className="drawer-body">
        {drawerView === "detail" ? (
          <>
            {node.kind === "llm" ? <DrawerLLMContent node={node} context={filteredContext} /> : null}
            {node.kind === "todo" ? <DrawerTodoContent node={node} todos={todos} /> : null}
            {node.kind === "error" ? <DrawerErrorContent node={node} context={filteredContext} /> : null}
          </>
        ) : (
          <AllContextView inputMessages={inputMessages} nodeOutput={nodeOutput} />
        )}
      </div>

      {/* 底部操作 — 仅 LLM/Tool 节点显示 */}
      {drawerView === "detail" && canShowContext ? (
        <div className="drawer-footer">
          <button
            className="drawer-jump-btn"
            type="button"
            onClick={() => setDrawerView("all")}
          >
            查看全部上下文
          </button>
        </div>
      ) : null}
    </aside>
  );
}

// ── 全部上下文视图 — 模型输入（messages） + 节点输出（从事件直接获取） ──

function AllContextView({ inputMessages, nodeOutput }: { inputMessages: unknown[] | null; nodeOutput: unknown[] | unknown | null }) {
  // 节点输出：消息数组（LLM）或任意 JSON（Tool）
  const outputIsMessages = Array.isArray(nodeOutput) && nodeOutput.length > 0 && typeof nodeOutput[0] === "object" && nodeOutput[0] !== null && ("type" in (nodeOutput[0] as Record<string, unknown>) || "role" in (nodeOutput[0] as Record<string, unknown>));

  return (
    <div className="checkpoint-messages">
      {/* 模型输入消息（系统提示词 + 注入上下文 + 对话历史） */}
      {inputMessages && inputMessages.length > 0 ? (
        <>
          <div className="checkpoint-section-label">模型输入</div>
          {inputMessages.map((msg, index) => {
            if (!msg || typeof msg !== "object") return null;
            const m = msg as Record<string, unknown>;
            return <ModelInputMessage key={`input-${index}`} message={m} />;
          })}
        </>
      ) : null}

      {/* 节点输出 */}
      {nodeOutput != null ? (
        <>
          <div className="checkpoint-section-label">节点输出</div>
          {outputIsMessages
            ? (nodeOutput as unknown[]).map((msg, index) => {
                if (!msg || typeof msg !== "object") return null;
                const m = msg as Record<string, unknown>;
                return <ModelInputMessage key={`output-${index}`} message={m} />;
              })
            : <pre className="checkpoint-msg-content">{typeof nodeOutput === "string" ? nodeOutput : JSON.stringify(nodeOutput, null, 2)}</pre>
          }
        </>
      ) : null}

      {!inputMessages?.length && nodeOutput == null ? (
        <p className="status-copy">无上下文数据。</p>
      ) : null}
    </div>
  );
}

/** 模型输入消息渲染 */
const MSG_KIND_CONFIG: Record<string, { label: string; className: string }> = {
  system: { label: "System", className: "system" },
  human: { label: "Human", className: "human" },
  ai: { label: "AI", className: "ai" },
  assistant: { label: "AI", className: "ai" },
  tool: { label: "Tool", className: "tool" },
};

function ModelInputMessage({ message }: { message: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const msgType = String(message.type ?? message.role ?? "unknown").toLowerCase();
  const config = MSG_KIND_CONFIG[msgType] ?? { label: msgType, className: "" };

  // 正确提取文本：处理 string / content blocks 数组 / 其他
  const rawContent = message.content;
  const text = extractMessageText(rawContent);
  const lines = text.split("\n");
  const shouldCollapse = lines.length > 10;
  const displayLines = shouldCollapse && !expanded ? lines.slice(0, 10) : lines;

  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : null;

  return (
    <article className={`checkpoint-msg ${config.className}`}>
      <header className="checkpoint-msg-header">
        <span className={`checkpoint-msg-badge ${config.className}`}>{config.label}</span>
        {message.name ? <span className="checkpoint-msg-tool-name">{String(message.name)}</span> : null}
        {msgType === "human" && text.length > 50 && text.includes("：\n") ? (
          <span className="checkpoint-msg-inject-tag">注入上下文</span>
        ) : null}
      </header>
      {text ? (
        <div className="checkpoint-msg-content md-render">
          {displayLines.map((line, i) => (
            <MarkdownLine key={i} line={line} />
          ))}
        </div>
      ) : null}
      {toolCalls && toolCalls.length > 0 ? (
        <div className="checkpoint-msg-tool-calls">
          {toolCalls.map((tc: unknown, i: number) => {
            const call = tc as Record<string, unknown>;
            return <span className="checkpoint-msg-tool-call-tag" key={i}>{String(call.name ?? "tool")}</span>;
          })}
        </div>
      ) : null}
      {shouldCollapse ? (
        <button className="checkpoint-msg-toggle" type="button" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "收起" : "展开全部"}
        </button>
      ) : null}
    </article>
  );
}

/** 从 content 中提取纯文本，处理 string / content blocks / 其他 */
function extractMessageText(content: unknown): string {
  if (content == null) return "";
  if (typeof content === "string") return content;
  // content blocks: [{"type": "text", "text": "..."}, ...]
  if (Array.isArray(content)) {
    return content
      .filter((block): block is Record<string, unknown> =>
        typeof block === "object" && block !== null && "text" in block
      )
      .map((block) => String(block.text))
      .join("\n");
  }
  return JSON.stringify(content, null, 2);
}

/** 渲染单行 — 把 # 标题、- 列表等转为 HTML */
function MarkdownLine({ line }: { line: string }) {
  // ### 标题
  const h3 = line.match(/^###\s+(.+)/);
  if (h3) return <div className="md-h3">{h3[1]}</div>;
  // ## 标题
  const h2 = line.match(/^##\s+(.+)/);
  if (h2) return <div className="md-h2">{h2[1]}</div>;
  // # 标题
  const h1 = line.match(/^#\s+(.+)/);
  if (h1) return <div className="md-h1">{h1[1]}</div>;
  // 空行
  if (line.trim() === "") return <div className="md-blank" />;
  // 普通行
  return <div className="md-text">{line}</div>;
}

// ── 节点详情视图 ──

/** LLM 展开 — 按 related_node_id 筛选 segments */
function DrawerLLMContent({ node, context }: { node: TraceNode; context: TraceContextSegment[] }) {
  const segments = context.filter(
    (seg) => seg.related_node_id === node.node_id
  );

  const anchorIds = new Set<string>();
  if (node.context_anchor_id) anchorIds.add(node.context_anchor_id);
  if (node.output_context_anchor_id) anchorIds.add(node.output_context_anchor_id);
  if (node.input_context_range?.start_anchor_id) anchorIds.add(node.input_context_range.start_anchor_id);
  if (node.input_context_range?.end_anchor_id) anchorIds.add(node.input_context_range.end_anchor_id);

  const allSegments = segments.length > 0
    ? segments
    : context.filter((seg) => anchorIds.has(seg.anchor_id));

  if (allSegments.length === 0) {
    return <p className="status-copy">无上下文数据。</p>;
  }

  return (
    <div className="drawer-segments">
      {allSegments.map((segment) => (
        <DrawerSegmentItem key={segment.anchor_id} segment={segment} />
      ))}
    </div>
  );
}

/** 单个 segment 渲染 */
function DrawerSegmentItem({ segment }: { segment: TraceContextSegment }) {
  const config = SEGMENT_KIND_CONFIG[segment.kind] ?? { label: segment.kind, className: "" };

  return (
    <article className={`drawer-segment ${config.className}`}>
      <header className="drawer-segment-header">
        <span className={`drawer-segment-badge ${config.className}`}>
          <SegmentIcon kind={segment.kind} size={12} />
        </span>
        <span className="drawer-segment-kind-label">{config.label}</span>
        <span className="drawer-segment-title">{segment.title}</span>
      </header>
      <DrawerSegmentContent value={segment.content} kind={segment.kind} />
    </article>
  );
}

/** segment 内容渲染 */
function DrawerSegmentContent({ value, kind }: { value: unknown; kind: string }) {
  if (kind === "todo" && Array.isArray(value)) {
    return (
      <ul className="trace-todo-list">
        {(value as TraceTodoItem[]).map((item, index) => (
          <li className={`trace-todo-item ${item.status}`} key={item.id ?? `${item.content}-${index}`}>
            <span>{todoStatusLabel(item.status)}</span>
            <p>{item.content}</p>
          </li>
        ))}
      </ul>
    );
  }

  if (typeof value === "string") {
    return <p className="drawer-segment-copy">{value || "空内容"}</p>;
  }

  return <pre className="drawer-segment-json">{JSON.stringify(value, null, 2)}</pre>;
}

/** Todo 展开 — 按 anchor_id 匹配 todo snapshot */
function DrawerTodoContent({
  node,
  todos,
}: {
  node: TraceNode;
  todos: { anchor_id: string; items: TraceTodoItem[]; active_item?: string | null }[];
}) {
  const anchorId = node.context_anchor_id || node.output_context_anchor_id;
  const snapshot = todos.find((t) => t.anchor_id === anchorId);

  const items = snapshot?.items ?? [];

  if (items.length === 0) {
    return <p className="status-copy">无任务数据。</p>;
  }

  return (
    <div className="drawer-todos">
      {snapshot?.active_item ? (
        <div className="drawer-todo-active">
          当前任务: <strong>{snapshot.active_item}</strong>
        </div>
      ) : null}
      <ul className="trace-todo-list">
        {items.map((item, index) => (
          <li className={`trace-todo-item ${item.status}`} key={item.id ?? `${item.content}-${index}`}>
            <span>{todoStatusLabel(item.status)}</span>
            <p>{item.content}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Error 展开 — 显示完整错误信息 */
function DrawerErrorContent({ node, context }: { node: TraceNode; context: TraceContextSegment[] }) {
  const errorText = node.error;
  const errorSegments = context.filter(
    (seg) => seg.related_node_id === node.node_id && seg.kind === "error"
  );

  return (
    <div className="drawer-error">
      {errorText ? (
        <div className="drawer-error-block">
          <pre className="drawer-error-stack">{errorText}</pre>
        </div>
      ) : null}
      {errorSegments.map((seg) => (
        <div className="drawer-error-block" key={seg.anchor_id}>
          <p className="drawer-error-copy">{typeof seg.content === "string" ? seg.content : JSON.stringify(seg.content, null, 2)}</p>
        </div>
      ))}
      {!errorText && errorSegments.length === 0 ? (
        <p className="status-copy">无错误详情。</p>
      ) : null}
    </div>
  );
}

function todoStatusLabel(status: string): string {
  if (status === "completed") return "完成";
  if (status === "in_progress") return "进行中";
  return "待办";
}

/** 从 raw_event_ids 中提取首个事件序号，如 ["trace-xx-4","trace-xx-5"] → "#4" */
function eventNumberLabel(rawEventIds?: string[]): string | null {
  if (!rawEventIds || rawEventIds.length === 0) return null;
  const id = rawEventIds[0];
  const parts = id.split("-");
  const n = parseInt(parts[parts.length - 1], 10);
  if (Number.isNaN(n)) return null;
  return `#${n}`;
}
