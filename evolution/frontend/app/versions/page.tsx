"use client";

/**
 * 配置版本谱系（/versions，需求 D8）。
 *
 * 两栏：
 *   左：版本谱系列表（按版本号倒序，parent_version 形成谱系链）
 *       production 高亮，retired 暗淡
 *   右：选中版本的 edits + manifest 详情（D11，不含完整 config JSON）
 *
 * 支持 ?v=N query 预选某版本（驾驶舱历轮表的跳转）。
 * 不做整 JSON diff（D11 决策）——只展示 evolver 本轮的 edits + reward 变化。
 */
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { fetchVersionDetail, fetchVersions } from "@/lib/adapt-api";
import type { AdaptEdit, VersionDetail, VersionListItem } from "@/lib/adapt-types";

export default function VersionsPage() {
  return (
    <Suspense fallback={<div className="text-dim" style={{ padding: 48 }}>加载中…</div>}>
      <VersionsInner />
    </Suspense>
  );
}

function VersionsInner() {
  const searchParams = useSearchParams();
  const initialV = searchParams.get("v");
  const [items, setItems] = useState<VersionListItem[]>([]);
  const [productionVersion, setProductionVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetchVersions({ limit: 200 });
      setItems(resp.items);
      setProductionVersion(resp.production_version);
      // 默认选中：优先 query 的 v，否则最新 production
      if (selected == null) {
        const v = initialV ? Number(initialV) : resp.production_version;
        if (v != null && !Number.isNaN(v)) setSelected(v);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 选中版本变化时拉详情
  useEffect(() => {
    if (selected == null) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    fetchVersionDetail(selected)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selected]);

  return (
    <div>
      <h1 className="page-title">配置版本谱系</h1>
      <p className="page-subtitle">
        每次进化 ship 产出一个新版本 · 当前线 v{productionVersion ?? "—"} · 共 {items.length} 版
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="versions-layout">
        {/* 左：版本链表 */}
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
                还没有版本。启动一次进化，第一次 ship 会生成 v1。
              </div>
            ) : (
              items.map((v, i) => {
                const isProd = v.version === productionVersion;
                const isSelected = v.version === selected;
                const prev = items[i + 1]; // 倒序列表，下一项是上一版
                const isLineageChild = prev && v.parent_version === prev.version;
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
                        {v.change_summary || "（无摘要）"}
                      </div>
                      <div className="version-foot mono text-mute">
                        {v.reward != null && (
                          <span style={{ color: "var(--completed)" }}>reward {v.reward.toFixed(3)}</span>
                        )}
                        {v.source_session && <span> · {v.source_session.slice(0, 8)}</span>}
                      </div>
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* 右：版本详情（edits + manifest，D11）*/}
        <div className="version-detail">
          {detailLoading ? (
            <div className="card" style={{ padding: 40, textAlign: "center" }}>
              <span className="text-mute">加载详情…</span>
            </div>
          ) : detail ? (
            <VersionDetailPanel detail={detail} />
          ) : (
            <div className="card" style={{ padding: 40, textAlign: "center" }}>
              <span className="text-dim">从左侧选择一个版本</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function VersionDetailPanel({ detail }: { detail: VersionDetail }) {
  const rewardDelta =
    detail.reward != null && detail.baseline_reward != null
      ? detail.reward - detail.baseline_reward
      : null;
  return (
    <div className="card version-detail-card">
      <div className="version-detail-head">
        <div>
          <h2 className="version-detail-title">
            v{detail.version}
            {detail.status === "production" && (
              <span className="version-prod-tag" style={{ marginLeft: 10 }}>PRODUCTION</span>
            )}
          </h2>
          <div className="version-detail-meta mono text-dim">
            parent {detail.parent_version != null ? `v${detail.parent_version}` : "（根）"}
            {detail.source_commit && ` · ${detail.source_commit.slice(0, 7)}`}
            {detail.is_bootstrap && " · bootstrap 生成"}
          </div>
        </div>
      </div>

      {detail.change_summary && (
        <div className="version-change-summary">{detail.change_summary}</div>
      )}

      {/* reward 变化 */}
      {(detail.reward != null || detail.baseline_reward != null) && (
        <div className="version-reward-row">
          <RewardStat label="baseline" value={detail.baseline_reward} />
          <RewardStat label="本版" value={detail.reward} accent />
          {rewardDelta != null && (
            <div className="reward-delta">
              <span className="text-mute mono" style={{ fontSize: 11 }}>变化</span>
              <span
                className="mono"
                style={{ color: rewardDelta >= 0 ? "var(--completed)" : "var(--failed)", fontWeight: 650 }}
              >
                {rewardDelta >= 0 ? "+" : ""}{rewardDelta.toFixed(3)}
              </span>
            </div>
          )}
        </div>
      )}

      {/* edits（D11：只展示 edits + manifest）*/}
      <div className="version-edits-section">
        <h3 className="section-title">
          edits（{detail.edits.length}）
        </h3>
        {detail.edits.length === 0 ? (
          <p className="text-dim" style={{ fontSize: 13 }}>
            {detail.is_bootstrap
              ? "bootstrap 生成，无 evolver edits。"
              : "无 edits 数据。"}
          </p>
        ) : (
          <div className="edits-list">
            {detail.edits.map((e, j) => (
              <EditDetailRow key={j} edit={e} />
            ))}
          </div>
        )}
      </div>

      {/* critic verdict（如有）*/}
      {detail.critic_verdict.feedback && (
        <div className="version-critic">
          <h3 className="section-title">critic 反馈</h3>
          <pre className="landscape-text">{detail.critic_verdict.feedback}</pre>
        </div>
      )}
    </div>
  );
}

function RewardStat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | null;
  accent?: boolean;
}) {
  return (
    <div className="reward-stat">
      <span className="text-mute mono" style={{ fontSize: 11 }}>{label}</span>
      <span
        className="mono"
        style={{ fontSize: 20, fontWeight: 700, color: accent ? "var(--accent)" : "var(--text)" }}
      >
        {value != null ? value.toFixed(3) : "—"}
      </span>
    </div>
  );
}

function EditDetailRow({ edit }: { edit: AdaptEdit }) {
  const opColor: Record<string, string> = {
    replace: "var(--accent)",
    insert: "var(--completed)",
    remove: "var(--failed)",
  };
  return (
    <div className="edit-row">
      <div className="edit-op-line">
        <span className="edit-op mono" style={{ color: opColor[edit.op] ?? "var(--text-dim)" }}>
          {edit.op}
        </span>
        <span className="edit-target mono">{edit.target.join(" / ")}</span>
      </div>
      {edit.manifest && (
        <div className="edit-manifest">
          {edit.manifest.intent && (
            <div className="manifest-line">
              <span className="manifest-key">意图</span>
              <span>{edit.manifest.intent}</span>
            </div>
          )}
          {(edit.manifest.expected_up?.length || edit.manifest.expected_down?.length) && (
            <div className="manifest-line">
              <span className="manifest-key">预期</span>
              <span className="mono" style={{ fontSize: 11 }}>
                ↑ {edit.manifest.expected_up?.join(", ") || "—"}　↓ {edit.manifest.expected_down?.join(", ") || "—"}
              </span>
            </div>
          )}
          {edit.manifest.rationale && (
            <div className="manifest-line">
              <span className="manifest-key">理由</span>
              <span className="text-dim">{edit.manifest.rationale}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
