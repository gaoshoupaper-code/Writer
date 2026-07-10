"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

// 进化端主导航（三功能解耦）。
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

const ADMIN_ITEMS = [
  { href: "/admin/users", label: "用户管理" },
  { href: "/admin/invite-codes", label: "邀请码" },
  { href: "/admin/credits", label: "积分流水" },
  { href: "/admin/credits/settings", label: "积分设置" },
];

// 顶栏导航（全局共享）。沿用 dark technical × Swiss grid。
export function TopNav() {
  const pathname = usePathname();
  const [isSuperAdmin, setIsSuperAdmin] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE_URL}/api/me`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => setIsSuperAdmin(Boolean(data?.is_super_admin)))
      .catch(() => setIsSuperAdmin(false));
  }, []);

  const isActive = (href: string, exact?: boolean) =>
    exact ? pathname === href : pathname.startsWith(href);

  return (
    <nav className="top-nav">
      <span className="top-nav-brand">
        <span className="top-nav-brand-mark">
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
      {isSuperAdmin &&
        ADMIN_ITEMS.map((item) => {
          const active = pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`top-nav-item ${active ? "top-nav-item-active" : ""}`}
              style={{
                color: active ? "var(--text)" : "var(--text-dim)",
                borderLeft: "1px solid var(--border)",
                paddingLeft: 12,
              }}
            >
              {item.label}
            </Link>
          );
        })}
      <a href="/legacy" className="top-nav-link-dim">
        旧版 →
      </a>
    </nav>
  );
}
