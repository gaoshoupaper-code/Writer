/**
 * 左栏：候选 trace 搜索 + 筛选 + 列表（FR-002）。
 *
 * - 搜索：匹配 session_name 或 trace_id，大小写不敏感
 * - 筛选：按 trace 状态（全部 / 运行中 / 已完成 / 已失败 / …）
 * - 排序：最近记录优先（started_at 降序）
 * - 所有候选始终可达：筛选不命中显示「无匹配记录 + 清除条件」，与「系统无候选」区分
 *
 * 搜索/筛选纯本地 state，不发请求（NFR-003：<100ms）。
 */

import { useMemo, useState } from "react";

import type { TraceListItem } from "@/lib/api";
import { Search, X } from "lucide-react";

import {
  filterTraces,
  getTraceStatusMeta,
  getTraceTitle,
  formatRelativeTime,
} from "./dossierHelpers";

interface DossierSidebarProps {
  traces: TraceListItem[];
  loading: boolean;
  selectedTraceId: string;
  onSelect: (traceId: string) => void;
}

export function DossierSidebar({
  traces,
  loading,
  selectedTraceId,
  onSelect,
}: DossierSidebarProps) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const visibleTraces = useMemo(
    () => filterTraces(traces, search, statusFilter),
    [traces, search, statusFilter],
  );

  // 可用状态选项（基于当前候选集合，避免列出无意义的空筛选项）
  const statusOptions = useMemo(() => {
    const present = new Set(traces.map((t) => t.status));
    const options: Array<{ value: string; label: string }> = [{ value: "all", label: "全部状态" }];
    for (const status of present) {
      options.push({ value: status, label: getTraceStatusMeta(status).label });
    }
    return options;
  }, [traces]);

  const hasFilter = search.trim() !== "" || statusFilter !== "all";
  const clearFilters = () => {
    setSearch("");
    setStatusFilter("all");
  };

  return (
    <aside className="dossiers-sidebar">
      <div className="dossiers-sidebar-head">
        <h3>候选 trace</h3>
        <span className="dossiers-count">{traces.length}</span>
      </div>

      <div className="dossiers-search">
        <Search className="dossiers-search-icon" size={14} aria-hidden />
        <input
          type="text"
          className="dossiers-search-input"
          placeholder="按名称或 ID 搜索"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="按名称或 ID 搜索候选 trace"
        />
        {search && (
          <button
            className="dossiers-search-clear"
            onClick={() => setSearch("")}
            aria-label="清除搜索"
          >
            <X size={14} />
          </button>
        )}
      </div>

      <select
        className="dossiers-filter-select"
        value={statusFilter}
        onChange={(e) => setStatusFilter(e.target.value)}
        aria-label="按状态筛选候选 trace"
      >
        {statusOptions.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      <div className="trace-list">
        {loading && <div className="trace-list-loading">加载中…</div>}

        {!loading && visibleTraces.length === 0 && (
          <div className="trace-list-empty">
            {traces.length === 0 ? (
              <p>暂无候选 trace</p>
            ) : (
              <>
                <p>无匹配记录</p>
                <button className="trace-list-clear" onClick={clearFilters}>
                  清除筛选条件
                </button>
              </>
            )}
          </div>
        )}

        {!loading &&
          visibleTraces.map((t) => {
            const meta = getTraceStatusMeta(t.status);
            const Icon = meta.icon;
            const isSelected = selectedTraceId === t.trace_id;
            return (
              <button
                key={t.trace_id}
                className={`trace-item ${isSelected ? "selected" : ""}`}
                onClick={() => onSelect(t.trace_id)}
                aria-current={isSelected ? "true" : undefined}
              >
                <div className="trace-item-head">
                  <span className="trace-item-title">{getTraceTitle(t)}</span>
                  <span className={`trace-status tone-${meta.tone}`}>
                    <Icon size={12} className={meta.tone === "info" ? "spin" : ""} aria-hidden />
                    {meta.label}
                  </span>
                </div>
                <div className="trace-item-meta">
                  <span className="trace-item-id" title={t.trace_id}>
                    {t.trace_id.slice(0, 16)}…
                  </span>
                  <span className="trace-item-time">{formatRelativeTime(t.started_at)}</span>
                </div>
              </button>
            );
          })}
      </div>
    </aside>
  );
}
