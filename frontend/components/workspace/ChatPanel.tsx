import { KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { ChatMessage, ThreadSummary } from "../../lib/types";
import { SessionMenu } from "./SessionMenu";
import { ToolTree } from "./ToolTree";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

type ChatPanelProps = {
  messages: ChatMessage[];
  prompt: string;
  loading: boolean;
  threads: ThreadSummary[];
  activeThreadId: string;
  hasActiveWorkspace: boolean;
  activeStyleName: string | null;
  sessionMenuOpen: boolean;
  creatingThread: boolean;
  deleting: boolean;
  onPromptChange: (prompt: string) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  onStop: () => void;
  onToggleSessionMenu: () => void;
  onCloseSessionMenu: () => void;
  onCreateThread: () => void;
  onSelectThread: (threadId: string) => void;
  onDeleteThread: (threadId: string) => void;
  onOpenStyleModal: () => void;
};

export function ChatPanel({
  messages,
  prompt,
  loading,
  threads,
  activeThreadId,
  hasActiveWorkspace,
  activeStyleName,
  sessionMenuOpen,
  creatingThread,
  deleting,
  onPromptChange,
  onSubmit,
  onStop,
  onToggleSessionMenu,
  onCloseSessionMenu,
  onCreateThread,
  onSelectThread,
  onDeleteThread,
  onOpenStyleModal,
}: ChatPanelProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [canExpand, setCanExpand] = useState(false);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;

    const lineHeight = Number.parseFloat(window.getComputedStyle(input).lineHeight);
    const collapsedHeight = lineHeight * 3;
    const expandedHeight = lineHeight * 30;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, expanded ? expandedHeight : collapsedHeight)}px`;
    setCanExpand(input.scrollHeight > collapsedHeight + 1);
  }, [expanded, prompt]);

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <section className="conversation-panel panel-surface" aria-label="创作对话">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Dialogue</span>
          <h2>创作对话</h2>
        </div>
        <div className="style-trigger-wrap">
          <button
            className={`style-trigger-button${activeStyleName ? " has-style" : ""}`}
            type="button"
            onClick={onOpenStyleModal}
            disabled={!hasActiveWorkspace}
          >
            <span className="style-trigger-icon">&#9998;</span>
            {activeStyleName ? <span className="style-trigger-label">{activeStyleName}</span> : <span className="style-trigger-label">写作风格</span>}
          </button>
        </div>
        <div className="session-actions">
          <button
            className="session-create-button"
            type="button"
            onClick={() => {
              onCreateThread();
              onCloseSessionMenu();
            }}
            disabled={creatingThread || !hasActiveWorkspace}
          >
            {creatingThread ? "创建中" : "新建会话"}
          </button>
          <SessionMenu
            threads={threads}
            activeThreadId={activeThreadId}
            disabled={!hasActiveWorkspace}
            open={sessionMenuOpen}
            deleting={deleting}
            loading={loading}
            onToggle={onToggleSessionMenu}
            onClose={onCloseSessionMenu}
            onSelectThread={onSelectThread}
            onDeleteThread={onDeleteThread}
          />
        </div>
      </div>

      <div className="message-list">
        {messages.map((message, index) => {
          const label = message.role === "assistant" ? "Agent" : "你";
          const isLastAssistant = message.role === "assistant" && index === messages.length - 1 && loading;

          return (
            <article
              className={`message ${message.role} animate-in fade-in-0 slide-in-from-bottom-2 duration-300`}
              key={`${message.role}-${index}`}
            >
              <span className="message-role">{label}</span>
              {message.role === "assistant" && message.tools?.length ? <ToolTree tools={message.tools} /> : null}
              <div className="message-content">
                {message.role === "assistant" && message.contentFormat === "markdown" ? (
                  <ReactMarkdown>{message.content}</ReactMarkdown>
                ) : (
                  <p>{message.content}</p>
                )}
              </div>
              {/* 最后一条 assistant 消息 + loading 态 → 显示 shimmer */}
              {isLastAssistant && !message.content ? (
                <div className="grid gap-2 mt-2">
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-2/3" />
                </div>
              ) : null}
            </article>
          );
        })}
      </div>

      <form className="chat-composer" onSubmit={onSubmit}>
        <div className={`chat-input-wrap${expanded ? " expanded" : ""}`}>
          <textarea
            ref={inputRef}
            className="chat-input"
            value={prompt}
            rows={1}
            onChange={(event) => onPromptChange(event.target.value)}
            onKeyDown={handleInputKeyDown}
          />
          {canExpand ? (
            <button
              className="chat-input-expand"
              type="button"
              onClick={() => setExpanded((current) => !current)}
              aria-label={expanded ? "缩小输入框" : "扩大输入框"}
              title={expanded ? "缩小输入框" : "扩大输入框"}
            >
              {expanded ? "↙" : "↗"}
            </button>
          ) : null}
        </div>
        <div className="composer-actions">
          {loading ? (
            <Button
              variant="outline"
              className="stop-button min-h-[46px] rounded-[14px] px-4 text-sm font-black text-red-700 border-red-200 hover:bg-red-50"
              type="button"
              onClick={onStop}
            >
              停止
            </Button>
          ) : null}
          <Button
            className="send-button min-h-[46px] rounded-[14px] px-4 text-sm font-black bg-gradient-to-br from-[var(--coral)] to-[var(--gold)] shadow-lg hover:shadow-xl hover:-translate-y-px transition-all"
            type="submit"
            disabled={loading}
          >
            {loading ? "生成中" : "发送"}
          </Button>
        </div>
      </form>
    </section>
  );
}
