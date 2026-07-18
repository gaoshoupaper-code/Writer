import { useEffect, useState, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getTraces,
  getUserCache,
  type TraceListItem,
  type UserCacheItem,
} from "@/lib/api";

/**
 * Trace 历史观测页。
 *
 * 双 Tab（与监测大盘一致）：
 * - 创作历史：run_purpose = user_generation（用户创作 trace），支持按用户筛选
 * - 进化历史：run_purpose = evolution_eval / evolution_evolve（进化端自产 trace）
 *
 * 过滤：时间范围预设（今天/7天/30天/全部）+ 用户下拉（仅创作 Tab）。
 * 分页：50 条/页，传统分页器。手动刷新（历史 trace 均为终态，无需自动刷新）。
 * 排序：固定 started_at 倒序（最新在上）。
 */
const PAGE_SIZE = 50;

type TabKind = "creation" | "evolution";
type TimeRange = "today" | "7d" | "30d" | "all";

export default function HistoryPage() {
  const navigate = useNavigate();

  const [tab, setTab] = useState<TabKind>("creation");
  const [timeRange, setTimeRange] = useState<TimeRange>("all");
  const [selectedUser, setSelectedUser] = useState<string>("");
  const [page, setPage] = useState(0);

  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [users, setUsers] = useState<UserCacheItem[]>([]);
  const [loading, setLoading] = useState(true);

  const runPurposes = useMemo(
    () =>
      tab === "creation"
        ? ["user_generation"]
        : ["evolution_eval", "evolution_evolve"],
    [tab],
  );

  // 时间范围 → since ISO 时间戳（until 不限，到当前时刻即可）
  const sinceISO = useMemo(() => {
    if (timeRange === "all") return undefined;
    const now = new Date();
    if (timeRange === "today") {
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      return start.toISOString();
    }
    const days = timeRange === "7d" ? 7 : 30;
    return new Date(now.getTime() - days * 86400_000).toISOString();
  }, [timeRange]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      // run_purpose 是单值过滤，进化 Tab 需分两次请求合并
      if (runPurposes.length === 1) {
        const resp = await getTraces({
          run_purpose: runPurposes[0],
          since: sinceISO,
          owner: tab === "creation" && selectedUser ? selectedUser : undefined,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        });
        setTraces(resp.items);
        setTotal(resp.total);
      } else {
        // 进化 Tab：eval + evolve 两次请求合并（小数据量，可接受）
        const [evalResp, evolveResp] = await Promise.all([
          getTraces({ run_purpose: "evolution_eval", since: sinceISO, limit: PAGE_SIZE }),
          getTraces({ run_purpose: "evolution_evolve", since: sinceISO, limit: PAGE_SIZE }),
        ]);
        const merged = [...evalResp.items, ...evolveResp.items]
          .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
        setTotal(merged.length);
        setTraces(merged.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE));
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取历史 trace 失败");
    } finally {
      setLoading(false);
    }
  }, [runPurposes, sinceISO, tab, selectedUser, page]);

  // 首次加载用户缓存列表（失败提示，不静默吞——否则用户筛选下拉消失无报错）
  useEffect(() => {
    getUserCache()
      .then(setUsers)
      .catch((err) => {
        setUsers([]);
        toast.error(err instanceof Error ? err.message : "用户列表加载失败");
      });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 切换 Tab / 时间范围 / 用户时回到第一页
  const switchTab = (t: TabKind) => {
    setTab(t);
    setPage(0);
    setSelectedUser("");
  };

  const switchTimeRange = (r: TimeRange) => {
    setTimeRange(r);
    setPage(0);
  };

  const switchUser = (u: string) => {
    setSelectedUser(u);
    setPage(0);
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="history-page">
      <header className="page-header">
        <h1>Trace 历史</h1>
        <p className="page-desc">浏览历史执行轨迹，按来源、时间、用户筛选</p>
      </header>

      {/* 双 Tab */}
      <nav className="inspection-tabs monitor-tabs">
        <button
          className={`inspection-tab ${tab === "creation" ? "active" : ""}`}
          type="button"
          onClick={() => switchTab("creation")}
        >
          创作历史
        </button>
        <button
          className={`inspection-tab ${tab === "evolution" ? "active" : ""}`}
          type="button"
          onClick={() => switchTab("evolution")}
        >
          进化历史
        </button>
      </nav>

      {/* 过滤栏 */}
      <div className="history-toolbar">
        <div className="history-filter-group">
          {(["today", "7d", "30d", "all"] as TimeRange[]).map((r) => (
            <button
              key={r}
              className={`history-range-btn ${timeRange === r ? "active" : ""}`}
              type="button"
              onClick={() => switchTimeRange(r)}
            >
              {r === "today" ? "今天" : r === "7d" ? "最近 7 天" : r === "30d" ? "最近 30 天" : "全部"}
            </button>
          ))}
        </div>
        {tab === "creation" && users.length > 0 && (
          <select
            className="history-user-select"
            value={selectedUser}
            onChange={(e) => switchUser(e.target.value)}
          >
            <option value="">全部用户</option>
            {users.map((u) => (
              <option key={u.user_id} value={u.user_id}>
                {u.username}
              </option>
            ))}
          </select>
        )}
        <button className="history-refresh" type="button" onClick={refresh}>
          🔄 刷新
        </button>
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="page-loading">加载中…</div>
      ) : traces.length === 0 ? (
        <div className="monitor-empty">暂无历史 trace</div>
      ) : (
        <table className="data-table history-table">
          <thead>
            <tr>
              <th>标识</th>
              <th>来源</th>
              {tab === "creation" && <th>用户</th>}
              <th>状态</th>
              <th>开始时间</th>
              <th>耗时</th>
              <th>规模</th>
            </tr>
          </thead>
          <tbody>
            {traces.map((t) => (
              <tr
                key={t.trace_id}
                className="history-row"
                onClick={() => navigate(`/traces/${t.trace_id}`)}
              >
                <td className="history-ident" title={t.trace_id}>
                  {t.session_name || t.trace_id.slice(0, 12)}
                </td>
                <td>
                  <PurposeBadge purpose={t.run_purpose} />
                </td>
                {tab === "creation" && (
                  <td className="history-user">
                    {t.owner_username || "—"}
                  </td>
                )}
                <td>
                  <span className={`run-status ${t.status}`}>● {t.status}</span>
                </td>
                <td className="history-time">
                  {t.started_at ? formatDateTime(t.started_at) : "—"}
                </td>
                <td>{formatMs(t.duration_ms)}</td>
                <td>{t.event_count} 事件</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* 分页器 */}
      {total > PAGE_SIZE && (
        <div className="history-pager">
          <button
            type="button"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            ← 上一页
          </button>
          <span className="history-pager-info">
            第 {page + 1} / {totalPages} 页 · 共 {total} 条
          </span>
          <button
            type="button"
            disabled={page >= totalPages - 1}
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
          >
            下一页 →
          </button>
        </div>
      )}
    </div>
  );
}

/** 来源标签（与 monitor.tsx 一致） */
function PurposeBadge({ purpose }: { purpose: string }) {
  const label =
    purpose === "evolution_eval" ? "评估"
      : purpose === "evolution_evolve" ? "进化"
      : purpose === "user_generation" ? "创作"
      : purpose;
  const color =
    purpose === "evolution_eval" ? "var(--running, #0f766e)"
      : purpose === "evolution_evolve" ? "#a78bfa"
      : "var(--muted)";
  return (
    <span className="purpose-badge" style={{ color }}>{label}</span>
  );
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatDateTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
