"use client";

/**
 * 进化总览（首页，需求 D7）。
 *
 * 用户打开进化端第一眼看到的页面。三块：
 *   1. 状态条：当前 production 版本 + 「启动进化」入口
 *   2. reward 趋势：横轴=配置版本（按时间），纵轴=shipped reward
 *   3. session 列表：最近 N 次 adapt，状态/轮数/shipped/版本
 *
 * 叙事重心：这不是"系统健康监测"，是"我的 AI 在怎么自我进化、效果如何"。
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { fetchSessions, fetchVersions } from "@/lib/adapt-api";
import { StartAdaptDialog } from "@/components/adapt/StartAdaptDialog";
import { SessionStatusBadge } from "@/components/adapt/SessionStatusBadge";
import { RewardChart } from "@/components/adapt/RewardChart";
import type {
  AdaptSessionListItem,
  VersionListItem,
} from "@/lib/adapt-types";

export default function EvolutionOverviewPage() {
  const [sessions, setSessions] = useState<AdaptSessionListItem[]>([]);
  const [versions, setVersions] = useState<VersionListItem[]>([]);
  const [productionVersion, setProductionVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showStart, setShowStart] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sessResp, verResp] = await Promise.all([
        fetchSessions({ limit: 50 }),
        fetchVersions({ limit: 100 }),
      ]);
      setSessions(sessResp.items);
      setVersions(verResp.items);
      setProductionVersion(verResp.production_version);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // reward 趋势：取有 reward 的 shipped 版本，按版本号（≈时间）正序
  const rewardPoints = versions
    .filter((v) => v.reward != null)
    .sort((a, b) => a.version - b.version)
    .map((v) => ({ version: v.version, reward: v.reward as number }));

  const totalSessions = sessions.length;
  const totalShipped = versions.filter((v) => v.status === "production" || v.reward != null).length;

  return (
    <div>
      <div className="overview-head">
        <div>
          <h1 className="page-title">进化总览</h1>
          <p className="page-subtitle">
            AEGIS 自驱进化循环 · 当前线 v{productionVersion ?? "—"}
          </p>
        </div>
        <button className="btn-primary" onClick={() => setShowStart(true)}>
          启动进化
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {/* 状态条：三个关键数字，左对齐，不做居中卡片墙 */}
      <div className="stat-row">
        <StatBlock label="当前 production" value={productionVersion != null ? `v${productionVersion}` : "—"} accent />
        <StatBlock label="累计版本" value={String(versions.length)} />
        <StatBlock label="近期 session" value={String(totalSessions)} />
        <StatBlock label="已 ship 改进" value={String(totalShipped)} />
      </div>

      {/* reward 趋势：占满宽度，体现"在变好"的叙事 */}
      <section style={{ marginTop: 24 }}>
        <div className="section-head">
          <h2 className="section-title">reward 趋势</h2>
          <span className="text-mute mono" style={{ fontSize: 11 }}>
            每个 shipped 版本的候选 reward
          </span>
        </div>
        <div className="card" style={{ padding: 0 }}>
          {loading ? (
            <div className="chart-skeleton" />
          ) : rewardPoints.length === 0 ? (
            <div className="empty-state">
              <p className="text-dim" style={{ fontSize: 13 }}>
                还没有 shipped 版本。启动一次进化，让 reward 曲线长出来。
              </p>
            </div>
          ) : (
            <RewardChart points={rewardPoints} />
          )}
        </div>
      </section>

      {/* session 列表 */}
      <section style={{ marginTop: 28 }}>
        <div className="section-head">
          <h2 className="section-title">进化 session</h2>
          <span className="text-mute mono" style={{ fontSize: 11 }}>
            最近 {sessions.length} 次 adapt
          </span>
        </div>
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>session</th>
                <th>状态</th>
                <th>轮次</th>
                <th>shipped</th>
                <th>版本</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={6} className="text-mute" style={{ textAlign: "center", padding: 32 }}>
                    加载中…
                  </td>
                </tr>
              ) : sessions.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-dim" style={{ textAlign: "center", padding: 32 }}>
                    还没有进化记录。点击右上「启动进化」开始第一次。
                  </td>
                </tr>
              ) : (
                sessions.map((s) => (
                  <tr
                    key={s.session_id}
                    onClick={() => (window.location.href = `/sessions/?id=${s.session_id}`)}
                    style={{ cursor: "pointer" }}
                  >
                    <td className="mono">{s.session_id}</td>
                    <td>
                      <SessionStatusBadge status={s.status} />
                    </td>
                    <td className="mono">{s.round_count}</td>
                    <td className="mono">{s.shipped_count}</td>
                    <td className="mono text-dim">
                      {s.shipped_version ? `v${s.baseline_version}→v${s.shipped_version}` : `v${s.baseline_version}`}
                    </td>
                    <td className="mono text-mute">{fmtTime(s.last_at)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {showStart && <StartAdaptDialog onClose={() => setShowStart(false)} />}
    </div>
  );
}

function StatBlock({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="stat-block">
      <div className="stat-label">{label}</div>
      <div
        className="stat-value mono"
        style={accent ? { color: "var(--accent)" } : undefined}
      >
        {value}
      </div>
    </div>
  );
}

function fmtTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // 紧凑相对：今天显示 HH:MM，否则 MM-DD HH:MM
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (sameDay) return hhmm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
}
