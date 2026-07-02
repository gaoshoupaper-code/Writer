"use client";

/**
 * 首页（/）—— 三功能导航 landing。
 *
 * 进化端三个对等功能入口：
 *   ① 手动测试：选数据集 + Agent 版本 → 跑一次 → 产 trace
 *   ② 评估：选一条 trace → 评估 Agent 诊断（评分+问题+证据）
 *   ③ 进化：选已评估的 trace → 进化 Agent 产改动 → 人工发版
 *
 * 数据流：测试产 trace → 评估诊断 → 进化改进 → 回测试验证（人工串联）
 */
import Link from "next/link";

const FEATURES = [
  {
    href: "/tests",
    num: "①",
    title: "单次测试",
    desc: "选数据集 + Agent 版本，调执行端跑一次生成，产出 trace。不自动进入进化。",
    cta: "去测试",
    accent: "var(--text-dim)",
  },
  {
    href: "/evaluation",
    num: "②",
    title: "评估",
    desc: "选一条 trace，评估 Agent 诊断 Agent 运行流程 + 内容质量，产出评估报告（评分+问题+证据）。",
    cta: "去评估",
    accent: "var(--warn)",
  },
  {
    href: "/evolve",
    num: "③",
    title: "进化系统",
    desc: "选一条已评估的 trace，进化 Agent 基于评估报告产出新 Agent 版本（方案→执行→人工发版）。",
    cta: "去进化",
    accent: "var(--accent)",
  },
];

export default function HomePage() {
  return (
    <div>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ margin: 0 }}>Writer 进化端</h1>
        <p className="text-dim" style={{ marginTop: 8 }}>
          双 Agent 架构：评估 Agent 诊断 trace · 进化 Agent 基于评估产新版本。三功能各自独立，人工串联闭环。
        </p>
      </div>

      {/* 三功能卡片 */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
          gap: 20,
        }}
      >
        {FEATURES.map((f) => (
          <Link
            key={f.href}
            href={f.href}
            className="card"
            style={{
              display: "block",
              textDecoration: "none",
              color: "inherit",
              padding: 24,
              borderTop: `3px solid ${f.accent}`,
              transition: "transform 0.12s ease, border-color 0.12s ease",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
              <span
                className="mono"
                style={{ fontSize: 24, color: f.accent, fontWeight: 700 }}
              >
                {f.num}
              </span>
              <h2 style={{ margin: 0, fontSize: 18 }}>{f.title}</h2>
            </div>
            <p className="text-dim" style={{ fontSize: 13, lineHeight: 1.6, margin: "0 0 16px" }}>
              {f.desc}
            </p>
            <span style={{ color: f.accent, fontSize: 13 }}>{f.cta} →</span>
          </Link>
        ))}
      </div>

      {/* 数据流说明 */}
      <div className="card" style={{ marginTop: 32, padding: 20 }}>
        <h3 style={{ marginTop: 0, fontSize: 14 }}>数据流</h3>
        <div className="mono text-dim" style={{ fontSize: 12, lineHeight: 2 }}>
          ① 单次测试 <span style={{ color: "var(--accent)" }}>→ 产 trace</span>
          {" → "}
          ② 评估 <span style={{ color: "var(--accent)" }}>→ 产评估报告</span>
          {" → "}
          ③ 进化系统 <span style={{ color: "var(--accent)" }}>→ 产新版本（待审）</span>
          {" → "}
          人工发版 <span style={{ color: "var(--accent)" }}>→ 回①验证</span>
        </div>
      </div>
    </div>
  );
}
