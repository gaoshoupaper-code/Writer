"use client";

/**
 * 版本树（左侧栏）。
 *
 * production 置顶钉死（永远在最上），其余按 version 倒序。
 * config_json=NULL 的版本已被后端过滤（snapshot_repo.list_snapshots 只返回有效行）。
 */
import type { SnapshotListItem } from "@/lib/harness-types";

interface Props {
  snapshots: SnapshotListItem[];
  productionVersion: number | null;
  selectedVersion: number | null;
  loading: boolean;
  onSelect: (version: number) => void;
}

export function VersionTree({
  snapshots,
  productionVersion,
  selectedVersion,
  loading,
  onSelect,
}: Props) {
  // production 单独置顶，其余按号倒序
  const production = snapshots.find((s) => s.version === productionVersion);
  const rest = snapshots
    .filter((s) => s.version !== productionVersion)
    .sort((a, b) => b.version - a.version);
  const ordered = production ? [production, ...rest] : rest;

  return (
    <div className="harness-tree card" style={{ padding: 0 }}>
      <div className="harness-tree-head">
        <span className="section-title" style={{ margin: 0 }}>版本</span>
        <span className="text-mute mono" style={{ fontSize: 11 }}>
          {snapshots.length} 版
        </span>
      </div>
      <div className="harness-tree-list">
        {loading ? (
          <div className="text-mute" style={{ padding: 24, textAlign: "center" }}>
            加载中…
          </div>
        ) : ordered.length === 0 ? (
          <div className="text-dim" style={{ padding: 24, textAlign: "center", fontSize: 13 }}>
            还没有版本。启动进化 ship 后会生成快照。
          </div>
        ) : (
          ordered.map((s) => {
            const isProd = s.version === productionVersion;
            const isSelected = s.version === selectedVersion;
            return (
              <button
                key={s.version}
                className={`harness-tree-item ${isSelected ? "selected" : ""} ${isProd ? "production" : ""}`}
                onClick={() => onSelect(s.version)}
              >
                <div className="harness-tree-item-head">
                  <span className="mono" style={{ fontWeight: 600 }}>v{s.version}</span>
                  {isProd && <span className="harness-prod-tag">PRODUCTION</span>}
                  {s.status === "retired" && !isProd && (
                    <span className="harness-retired-tag mono">retired</span>
                  )}
                </div>
                <div className="harness-tree-summary text-dim">
                  {s.change_summary || "（无摘要）"}
                </div>
                <div className="harness-tree-foot mono text-mute">
                  {s.source_commit ? s.source_commit.slice(0, 7) : "无源码"}
                  {" · "}
                  {new Date(s.created_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
