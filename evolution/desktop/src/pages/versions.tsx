import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getVersions, type VersionListItem } from "@/lib/api";

/**
 * 版本谱系页（/versions）。
 *
 * 展示 harness 包的版本谱系链：左版本列表（按号倒序，parent_version 连线），
 * 右选中版本的概要（change_summary + 元信息）。
 *
 * 范围说明（2026-07-18）：
 * 本期只做"谱系浏览"——基于 registry 数据（version/parent/status/change_summary）。
 * 不做"版本间 diff"——因为 version_changes 表的写入层尚未实现（adapt→evolve 重构遗留），
 * 完整 diff（prompt ±N 行 / skills 增删 / middleware 改动）待后续修复数据层后补。
 * harness 页的"升级总览"也受同样限制，一并待修。
 *
 * 交互：
 *   - 选版本（左侧点击）→ 右侧显示概要
 *   - production 版本高亮（★）
 *   - retired 版本暗淡
 *   - URL hash 不带版本号（desktop 用 HashRouter，版本选择不进 URL，刷新重置）
 */

/** 选中版本的概要展示（本期只显示 registry 元信息，无 diff） */
function VersionSummary({ item }: { item: VersionListItem }) {
  return (
    <div className="card version-summary-card">
      <div className="version-summary-head">
        <h2>
          v{item.version}
          {item.status === "production" && (
            <span className="version-prod-tag" style={{ marginLeft: 10 }}>PRODUCTION</span>
          )}
          {item.status === "retired" && (
            <span className="version-retired-tag mono" style={{ marginLeft: 10 }}>retired</span>
          )}
        </h2>
        <div className="version-summary-meta mono text-dim">
          parent {item.parent_version != null ? `v${item.parent_version}` : "（根版本）"}
          {item.source_session && ` · session ${item.source_session.slice(0, 8)}`}
          {` · ${item.created_at.slice(0, 10)}`}
        </div>
      </div>

      {item.change_summary ? (
        <div className="version-change-summary">{item.change_summary}</div>
      ) : (
        <p className="text-dim" style={{ fontSize: 13, margin: "12px 0 0" }}>
          （无版本说明）
        </p>
      )}

      <div className="version-diff-placeholder">
        <p className="text-mute" style={{ fontSize: 12, margin: 0 }}>
          版本间要素 diff 待 version_changes 写入层修复后支持
          （adapt→evolve 重构遗留，harness 页"升级总览"同受此限）。
        </p>
      </div>
    </div>
  );
}

export default function VersionsPage() {
  const [items, setItems] = useState<VersionListItem[]>([]);
  const [productionVersion, setProductionVersion] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await getVersions();
      setItems(resp.items);
      setProductionVersion(resp.production_version);
      // 默认选中 production（无则最新）
      setSelected((prev) => prev ?? resp.production_version ?? resp.items[0]?.version ?? null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载版本列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const selectedItem = items.find((v) => v.version === selected) ?? null;

  return (
    <div className="versions-page">
      <header className="page-header">
        <div className="page-header-row">
          <div>
            <h1>版本谱系</h1>
            <p className="page-desc">
              harness 包版本链 · 当前生产 v{productionVersion ?? "—"} · 共 {items.length} 版
            </p>
          </div>
          <button
            className="action-link refresh-btn"
            onClick={load}
            title="刷新版本列表"
          >
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </header>

      <div className="versions-layout">
        {/* 左：版本链 */}
        <div className="versions-list card" style={{ padding: 0 }}>
          <div className="versions-list-head">
            <span className="section-title" style={{ margin: 0 }}>版本</span>
            <span className="text-mute mono" style={{ fontSize: 11 }}>parent → child</span>
          </div>
          <div className="versions-chain">
            {loading ? (
              <div className="text-mute" style={{ padding: 24, textAlign: "center" }}>
                加载中…
              </div>
            ) : items.length === 0 ? (
              <div className="text-dim" style={{ padding: 24, textAlign: "center", fontSize: 13 }}>
                还没有版本。发版后会在此显示。
              </div>
            ) : (
              items.map((v, i) => {
                const isProd = v.version === productionVersion;
                const isSelected = v.version === selected;
                // 倒序列表，下一项（i+1）是父版本；若本版 parent 指向下一项，画连线
                const parent = items[i + 1];
                const isLineageChild = parent && v.parent_version === parent.version;
                return (
                  <div key={v.version}>
                    {isLineageChild && <div className="versions-link-line" />}
                    <button
                      className={`version-item ${isSelected ? "selected" : ""} ${isProd ? "production" : ""}`}
                      onClick={() => setSelected(v.version)}
                    >
                      <div className="version-item-head">
                        <span className="version-num mono">v{v.version}</span>
                        {isProd && <span className="version-prod-tag">PRODUCTION</span>}
                        {v.status === "retired" && !isProd && (
                          <span className="version-retired-tag mono">retired</span>
                        )}
                      </div>
                      <div className="version-summary text-dim">
                        {v.change_summary?.slice(0, 60) || "（无摘要）"}
                      </div>
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* 右：版本概要 */}
        <div className="version-detail">
          {selectedItem ? (
            <VersionSummary item={selectedItem} />
          ) : (
            <div className="card" style={{ padding: 40, textAlign: "center" }}>
              <span className="text-dim">
                {loading ? "加载中…" : "从左侧选择一个版本"}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
