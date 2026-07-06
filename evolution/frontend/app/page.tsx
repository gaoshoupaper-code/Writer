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
    num: "01",
    title: "单次测试",
    desc: "选数据集 + Agent 版本，调执行端跑一次生成，产出 trace。不自动进入进化。",
    cta: "去测试",
    accent: "var(--text-dim)",
  },
  {
    href: "/evaluation",
    num: "02",
    title: "评估",
    desc: "选一条 trace，评估 Agent 诊断 Agent 运行流程与内容质量，产出评估报告（评分 + 问题 + 证据）。",
    cta: "去评估",
    accent: "var(--warn)",
  },
  {
    href: "/evolve",
    num: "03",
    title: "进化系统",
    desc: "选一条已评估的 trace，进化 Agent 基于评估报告产出新 Agent 版本（方案 → 执行 → 人工发版）。",
    cta: "去进化",
    accent: "var(--accent)",
  },
];

const FLOW = [
  { num: "①", label: "单次测试", out: "产 trace" },
  { num: "②", label: "评估", out: "产评估报告" },
  { num: "③", label: "进化系统", out: "产新版本（待审）" },
  { num: "↻", label: "人工发版", out: "回 ① 验证" },
];

export default function HomePage() {
  return (
    <div>
      {/* 页头：明确层级，左对齐，accent 细线收尾 */}
      <div className="landing-head">
        <h1>Writer 进化端</h1>
        <p className="landing-sub">
          双 Agent 架构：评估 Agent 诊断 trace，进化 Agent 基于评估产新版本。三功能各自独立，人工串联闭环。
        </p>
      </div>

      {/* 三功能卡：纵向编号 + 顶部语义色细条 */}
      <div className="feature-grid">
        {FEATURES.map((f) => (
          <Link
            key={f.href}
            href={f.href}
            className="feature-card"
            style={{ ["--card-accent" as string]: f.accent }}
          >
            <div className="feature-card-num">{f.num}</div>
            <h2 className="feature-card-title">{f.title}</h2>
            <p className="feature-card-desc">{f.desc}</p>
            <span className="feature-card-cta">{f.cta} →</span>
          </Link>
        ))}
      </div>

      {/* 数据流：横向流程图 */}
      <div className="flow-card">
        <h3>数据流</h3>
        <div className="flow-chain">
          {FLOW.map((s, i) => (
            <span key={s.label} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="flow-step">
                <span className="flow-step-num">{s.num}</span>
                {s.label}
                <span className="flow-step-out">→ {s.out}</span>
              </span>
              {i < FLOW.length - 1 && <span className="flow-arrow">›</span>}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
