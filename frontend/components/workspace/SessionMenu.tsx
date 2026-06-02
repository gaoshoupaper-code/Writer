import type { ThreadSummary } from "../../lib/types";

function sessionDisplayName(sessionName: string) {
  return sessionName.trim().slice(0, 15) || "未命名会话";
}

type SessionMenuProps = {
  threads: ThreadSummary[];
  activeThreadId: string;
  disabled: boolean;
  open: boolean;
  deleting: boolean;
  loading: boolean;
  onToggle: () => void;
  onClose: () => void;
  onSelectThread: (threadId: string) => void;
  onDeleteThread: (threadId: string) => void;
};

export function SessionMenu({
  threads,
  activeThreadId,
  disabled,
  open,
  deleting,
  loading,
  onToggle,
  onClose,
  onSelectThread,
  onDeleteThread,
}: SessionMenuProps) {
  return (
    <div className="session-menu">
      <button className="session-menu-trigger" type="button" onClick={onToggle} disabled={disabled}>
        <span>历史会话</span>
        <span className="session-menu-caret">▾</span>
      </button>
      {open ? (
        <div className="session-menu-popover">
          <div className="session-option-list">
            {threads.length ? (
              threads.map((thread) => (
                <div className={`session-option ${thread.thread_id === activeThreadId ? "active" : ""}`} key={thread.thread_id}>
                  <button
                    className="session-option-main"
                    type="button"
                    title={thread.session_name}
                    onClick={() => {
                      onSelectThread(thread.thread_id);
                      onClose();
                    }}
                  >
                    {sessionDisplayName(thread.session_name)}
                  </button>
                  <button
                    className="session-option-delete"
                    type="button"
                    onClick={() => onDeleteThread(thread.thread_id)}
                    disabled={deleting || loading}
                    aria-label={`删除 ${thread.session_name}`}
                  >
                    删除
                  </button>
                </div>
              ))
            ) : (
              <p className="session-empty">当前工作目录还没有会话。</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
