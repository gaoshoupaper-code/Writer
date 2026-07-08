import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { fetchMeOrNull, logout, type AuthMe } from "@/lib/api";

/**
 * Evolution 桌面端主布局（桌面化改造 2026-07-07）。
 *
 * 侧栏导航 + 内容区。登录守卫：未登录跳 /login。
 * 信息架构（设计文档）：监测 / 配置 已实现；核心工作区 / 试验台 Phase 5 扩展。
 */
const NAV_ITEMS = [
  { to: "/", label: "监测", end: true, icon: "📊" },
  { to: "/evaluation", label: "评估", end: false, icon: "📝" },
  { to: "/evolve", label: "进化", end: false, icon: "🧬" },
  { to: "/harness", label: "Agent 要素", end: false, icon: "🔧" },
  { to: "/tests", label: "单次测试", end: false, icon: "🧪" },
  { to: "/config", label: "配置", end: false, icon: "⚙️" },
];

export default function Shell() {
  const navigate = useNavigate();
  const [me, setMe] = useState<AuthMe | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    fetchMeOrNull().then((user) => {
      if (!user) {
        // 本地 dev 模式：executor 未启动时绕过登录，用 mock 用户直接进。
        // 生产构建（import.meta.env.DEV === false）仍强制登录。
        if (import.meta.env.DEV) {
          setMe({ user_id: "dev-local", username: "本地开发", is_admin: true, has_api_key: false });
          setChecking(false);
          return;
        }
        navigate("/login", { replace: true });
        return;
      }
      setMe(user);
      setChecking(false);
    });
  }, [navigate]);

  if (checking || !me) {
    return <div className="shell-loading">加载中…</div>;
  }

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
          <span className="shell-brand-text">Writer 进化</span>
        </div>
        <nav className="shell-nav">
          {NAV_ITEMS.map((item) =>
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
        <Outlet />
      </main>
    </div>
  );
}
