import { useCallback, useEffect, useState } from "react";
import { getEvolveSessions, type EvolveSession } from "@/lib/api";

/**
 * 进化历史 Tab（2026-07-20 新建，从 WorkbenchTab 左栏迁移而来）。
 *
 * 纯 session 列表展示——点击 → onSelect(session) → EvolvePage 写 URL 切回工作台。
 * 复用现有 getEvolveSessions + 10s 轮询（与原 WorkbenchTab 左栏一致），不重复造轮子。
 *
 * 与 /history 页的「进化历史」tab（trace 粒度，run_purpose=evolution_*）是两件事：
 *   - 本 tab：evolve session 粒度（一次进化共创会话）
 *   - /history 页：trace 粒度（一次执行轨迹）
 * 两者数据源不同（getEvolveSessions vs getTraces），用途互补不冲突。
 *
 * 空态：无 session 时引导去工作台启动（不在本 tab 提供启动按钮——启动入口在工作台中栏）。
 */
const STATUS_DOT: Record<string, string> = {
  running: "●",
  conversing: "●",
  finalizing: "●",
  pending_review: "◆",
  published: "✓",
  discarded: "✗",
  failed: "!",
  cancelled: "○",
};

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}

export default function HistoryTab({
  onSelect,
}: {
  onSelect: (session: EvolveSession) => void;
}) {
  const [sessions, setSessions] = useState<EvolveSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const resp = await getEvolveSessions(50);
      setSessions(resp.sessions);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载历史失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading && sessions.length === 0) {
    return (
      <div className="evolve-history evolve-history-loading">
        <p className="loading-hint">加载历史会话…</p>
      </div>
    );
  }

  if (error && sessions.length === 0) {
    return (
      <div className="evolve-history evolve-history-error">
        <p className="error-text">⚠ {error}</p>
      </div>
    );
  }

  return (
    <div className="evolve-history">
      <header className="history-header">
        <h3 className="history-title">进化历史</h3>
        <span className="history-count">{sessions.length} 个会话</span>
      </header>

      {sessions.length === 0 ? (
        <div className="history-empty">
          <p>还没有进化记录。</p>
          <p className="history-empty-hint">
            去「进化工作台」选一个评估完成的 trace，启动第一次进化共创。
          </p>
        </div>
      ) : (
        <ul className="session-list">
          {sessions.map((s) => (
            <li
              key={s.session_id}
              className="session-item"
              onClick={() => onSelect(s)}
            >
              <div className="session-item-head">
                <span className={`session-status status-${s.status}`}>
                  {STATUS_DOT[s.status] ?? "?"}
                </span>
                <code className="session-id">{s.session_id.slice(0, 8)}</code>
                <span className={`session-status-label status-${s.status}`}>
                  {s.status}
                </span>
              </div>
              <time className="session-time">{formatTime(s.created_at)}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
