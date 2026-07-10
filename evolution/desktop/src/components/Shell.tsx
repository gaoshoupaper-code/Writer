import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { fetchMeOrNull, logout, type AuthMe } from "@/lib/api";
import UpdateBanner from "@/components/UpdateBanner";

/**
 * Evolution 桌面端主布局（桌面化改造 2026-07-07）。
 *
 * 侧栏导航 + 内容区。登录守卫：未登录跳 /login。
 * 信息架构（设计文档）：监测 / 配置 已实现；核心工作区 / 试验台 Phase 5 扩展。
 */
const BASE_NAV_ITEMS = [
  { to: "/", label: "监测", end: true, icon: "📊" },
  { to: "/evaluation", label: "评估", end: false, icon: "📝" },
  { to: "/evolve", label: "进化", end: false, icon: "🧬" },
  { to: "/harness", label: "Agent 要素", end: false, icon: "🔧" },
  { to: "/dataset", label: "数据集", end: false, icon: "🗂️" },
  { to: "/tests", label: "单次测试", end: false, icon: "🧪" },
  { to: "/config", label: "配置", end: false, icon: "⚙️" },
];

// 管理后台导航（仅 super_admin 可见）。
const ADMIN_NAV_ITEMS = [
  { to: "/admin/users", label: "用户管理", end: false, icon: "👤" },
  { to: "/admin/invite-codes", label: "邀请码", end: false, icon: "🎟️" },
  { to: "/admin/credits", label: "积分流水", end: false, icon: "💧" },
  { to: "/admin/credits/settings", label: "积分设置", end: false, icon: "🎛️" },
];

export default function Shell() {
  const navigate = useNavigate();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    // 守卫重试：fetchMeOrNull 失败后重试 2 次（间隔 1s/2s），
    // 全部失败才跳登录。避免 executor 瞬时抖动 → 误跳登录页闪烁。
    async function checkAuth() {
      for (let attempt = 0; attempt < 3; attempt++) {
        const user = await fetchMeOrNull();
        if (user) {
          setMe(user);
          setChecking(false);
          return;
        }
        if (attempt < 2) await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
      }
      // 全部失败：dev 模式用 mock 绕过，生产跳登录
      if (import.meta.env.DEV) {
        setMe({ user_id: "dev-local", username: "本地开发", is_admin: true, is_super_admin: true, has_api_key: false });
        setChecking(false);
        return;
      }
      navigate("/login", { replace: true });
    }
    checkAuth();
  }, [navigate]);

  if (checking || !me) {
    return <div className="shell-loading">加载中…</div>;
  }

  const navItems = me.is_super_admin
    ? [...BASE_NAV_ITEMS, ...ADMIN_NAV_ITEMS]
    : BASE_NAV_ITEMS;

  async function handleLogout() {
    try {
      await logout();
    } catch {
      // 忽略——本地 cookie 已清，直接跳登录
    }
    navigate("/login", { replace: true });
  }

  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <div className="shell-brand">
          <span className="shell-brand-icon">🧬</span>
          <span className="shell-brand-text">思衍进化</span>
        </div>
        <nav className="shell-nav">
          {navItems.map((item) =>
            item.to ? (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `shell-nav-item ${isActive ? "active" : ""}`
                }
              >
                <span className="shell-nav-icon">{item.icon}</span>
                <span>{item.label}</span>
              </NavLink>
            ) : (
              <div key={item.label} className="shell-nav-item soon" title="即将上线">
                <span className="shell-nav-icon">{item.icon}</span>
                <span>{item.label}</span>
                <span className="shell-nav-badge">soon</span>
              </div>
            )
          )}
        </nav>
        <div className="shell-user">
          <div className="shell-user-info">
            <span className="shell-user-name">{me.username}</span>
            <span className="shell-user-id">{me.user_id.slice(0, 8)}</span>
          </div>
          <button className="shell-logout" onClick={handleLogout} title="退出登录">
            退出
          </button>
        </div>
      </aside>
      <main className="shell-main">
        <UpdateBanner />
        <Outlet />
      </main>
    </div>
  );
}
