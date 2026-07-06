"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// 进化端主导航（三功能解耦）。
// 首页(landing) + 三功能对等入口 + 辅助页。
// 三功能各自独立：单次测试→评估Agent→进化Agent。
const NAV_ITEMS = [
  { href: "/", label: "首页", exact: true },
  { href: "/tests", label: "单次测试" },
  { href: "/evaluation", label: "评估" },
  { href: "/evolve", label: "进化" },
  { href: "/traces", label: "Trace" },
  { href: "/monitor", label: "监测" },
  { href: "/versions", label: "版本谱系" },
  { href: "/harness", label: "Agent 要素" },
];

// 顶栏导航（全局共享）。沿用 dark technical × Swiss grid。
export function TopNav() {
  const pathname = usePathname();

  const isActive = (href: string, exact?: boolean) =>
    exact ? pathname === href : pathname.startsWith(href);

  return (
    <nav className="top-nav">
      <span className="top-nav-brand">
        <span className="top-nav-brand-mark">
          {/* 进化标记：一个螺旋收敛的符号，呼应 adapt loop 的迭代收敛 */}
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path
              d="M8 1.5a6.5 6.5 0 1 0 0 13"
              stroke="var(--accent)"
              strokeWidth="1.4"
              strokeLinecap="round"
              opacity="0.55"
            />
            <path
              d="M8 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0 2.2a1.8 1.8 0 1 1 0 3.6 1.8 1.8 0 0 1 0-3.6Z"
              fill="var(--accent)"
            />
          </svg>
        </span>
        <span className="top-nav-brand-text">Writer 进化</span>
      </span>
      {NAV_ITEMS.map((item) => {
        const active = isActive(item.href, item.exact);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`top-nav-item ${active ? "top-nav-item-active" : ""}`}
            style={active ? undefined : { color: "var(--text-dim)" }}
          >
            {item.label}
          </Link>
        );
      })}
      <div style={{ flex: 1 }} />
      <a href="/legacy" className="top-nav-link-dim">
        旧版 →
      </a>
    </nav>
  );
}
