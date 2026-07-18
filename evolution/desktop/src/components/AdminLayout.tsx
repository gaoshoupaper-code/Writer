import { NavLink, Outlet, Navigate } from "react-router-dom";

/**
 * 管理后台 Layout（D11 + D13，scope 分家 2026-07-18）。
 *
 * 把原平级的 4 个 admin 路由（用户/邀请码/积分流水/积分设置）收敛到一个父入口下，
 * 顶部 tab 切换。tab 与路由同步（D13=9b）：
 *   - 点 tab = 路由跳转，URL 仍各自独立（/admin/users 等可直链）
 *   - 进入 /admin 自动重定向到 /admin/users（默认 tab）
 *
 * 4 个子页面的业务逻辑不动，只加这一层布局壳。
 */

const ADMIN_TABS = [
  { to: "/admin/users", label: "用户" },
  { to: "/admin/invite-codes", label: "邀请码" },
  { to: "/admin/credits", label: "积分流水" },
  // 积分设置是 /admin/credits/settings，用 end=false 让 /admin/credits 和 /admin/credits/settings
  // 都能高亮"积分"区——但这里我们想让 settings 单独一个 tab，所以拆开：
  { to: "/admin/credits/settings", label: "积分设置" },
];

export default function AdminLayout() {
  return (
    <div className="admin-layout">
      <header className="page-header">
        <h1>管理后台</h1>
        <p className="page-desc">
          用户与积分管理。用户管理可调整积分、重置密码、禁启用账户；邀请码控制注册；
          积分流水查看所有变动；积分设置配置积分规则。
        </p>
      </header>
      <nav className="admin-tabs">
        {ADMIN_TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            // 积分流水(/admin/credits) 和 积分设置(/admin/credits/settings) 是父子路径，
            // 用 end 让"积分流水"只在精确匹配时高亮，避免被 settings 抢走高亮。
            end={tab.to === "/admin/credits"}
            className={({ isActive }) =>
              `admin-tab ${isActive ? "active" : ""}`
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>
      <div className="admin-content">
        <Outlet />
      </div>
    </div>
  );
}
