import { useEffect } from "react";
import { HashRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { setUnauthorizedHandler } from "@/lib/api";
import Home from "@/pages/home";
import Login from "@/pages/login";
import Register from "@/pages/register";
import Settings from "@/pages/settings";
import Skills from "@/pages/skills";

/// 401 跳转处理：api.ts 检测到 401 → 触发此 handler → 跳 /login。
/// 用组件内 hook（而非模块级 navigate），确保 Router context 可用。
function UnauthorizedHandler() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      // 避免在 /login /register 自身循环跳转
      if (location.pathname !== "/login" && location.pathname !== "/register") {
        navigate("/login", { replace: true });
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate, location.pathname]);
  return null;
}

/// Tauri 用 HashRouter：WebView 协议是 tauri://localhost，
/// BrowserRouter 依赖 history API 且需服务端兜底，HashRouter 纯客户端最稳。
function App() {
  return (
    <HashRouter>
      <UnauthorizedHandler />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/skills" element={<Skills />} />
        {/* 兜底：未知路由回首页 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster />
    </HashRouter>
  );
}

export default App;
