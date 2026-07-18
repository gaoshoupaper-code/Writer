import { useEffect } from "react";
import { HashRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { setUnauthorizedHandler } from "@/lib/api";
import Login from "@/pages/login";
import EvolutionConfigPage from "@/pages/config/EvolutionConfigPage";
import ExecutorConfigPage from "@/pages/config/ExecutorConfigPage";
import Monitor from "@/pages/monitor";
import Evolve from "@/pages/evolve";
import ReviewReport from "@/pages/review-report";
import Evaluation from "@/pages/evaluation";
import Harness from "@/pages/harness";
import Versions from "@/pages/versions";
import Dataset from "@/pages/dataset";
import Tests from "@/pages/tests";
import TraceDetail from "@/pages/trace-detail";
import History from "@/pages/history";
import AdminUsers from "@/pages/admin/users";
import AdminInviteCodes from "@/pages/admin/invite-codes";
import AdminCredits from "@/pages/admin/credits";
import AdminCreditsSettings from "@/pages/admin/credits/settings";
import Shell from "@/components/Shell";
import AdminLayout from "@/components/AdminLayout";

function UnauthorizedHandler() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (location.pathname !== "/login") {
        navigate("/login", { replace: true });
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate, location.pathname]);
  return null;
}

/// Evolution 桌面端路由（scope 分家 + 管理后台收敛，2026-07-18）。
/// 信息架构：基础 8 项不动 + 配置拆两（进化端模型/执行端模型）+ 管理后台 tab 化。
function App() {
  return (
    <HashRouter>
      <UnauthorizedHandler />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Shell />}>
          {/* 基础 8 项（D24：不动） */}
          <Route path="/" element={<Monitor />} />
          <Route path="/evolve" element={<Evolve />} />
          <Route path="/evolve/:sessionId/review" element={<ReviewReport />} />
          <Route path="/evaluation" element={<Evaluation />} />
          <Route path="/harness" element={<Harness />} />
          <Route path="/versions" element={<Versions />} />
          <Route path="/dataset" element={<Dataset />} />
          <Route path="/tests" element={<Tests />} />
          <Route path="/history" element={<History />} />

          {/* 配置拆两个（D17），老 /config 重定向到 /config/evolution 保兼容 */}
          <Route path="/config/evolution" element={<EvolutionConfigPage />} />
          <Route path="/config/executor" element={<ExecutorConfigPage />} />
          <Route path="/config" element={<Navigate to="/config/evolution" replace />} />

          {/* 管理后台嵌套 Layout（D11 + D13），仅超管可见（守卫在 Shell.tsx 菜单层） */}
          <Route path="/admin" element={<AdminLayout />}>
            <Route index element={<Navigate to="/admin/users" replace />} />
            <Route path="users" element={<AdminUsers />} />
            <Route path="invite-codes" element={<AdminInviteCodes />} />
            <Route path="credits" element={<AdminCredits />} />
            <Route path="credits/settings" element={<AdminCreditsSettings />} />
          </Route>

          <Route path="/traces/:traceId" element={<TraceDetail />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster />
    </HashRouter>
  );
}

export default App;
