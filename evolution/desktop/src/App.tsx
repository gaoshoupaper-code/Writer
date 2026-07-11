import { useEffect } from "react";
import { HashRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { setUnauthorizedHandler } from "@/lib/api";
import Login from "@/pages/login";
import Config from "@/pages/config";
import Monitor from "@/pages/monitor";
import Evolve from "@/pages/evolve";
import Evaluation from "@/pages/evaluation";
import Harness from "@/pages/harness";
import Dataset from "@/pages/dataset";
import Tests from "@/pages/tests";
import TraceDetail from "@/pages/trace-detail";
import History from "@/pages/history";
import AdminUsers from "@/pages/admin/users";
import AdminInviteCodes from "@/pages/admin/invite-codes";
import AdminCredits from "@/pages/admin/credits";
import AdminCreditsSettings from "@/pages/admin/credits/settings";
import Shell from "@/components/Shell";

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

/// Evolution 桌面端路由（桌面化改造 2026-07-07）。
/// 信息架构（设计文档）：监测首屏 / 核心工作区(评估+进化+要素) / 试验台(测试) / 配置。
function App() {
  return (
    <HashRouter>
      <UnauthorizedHandler />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Shell />}>
          <Route path="/" element={<Monitor />} />
          <Route path="/evolve" element={<Evolve />} />
          <Route path="/evaluation" element={<Evaluation />} />
          <Route path="/harness" element={<Harness />} />
          <Route path="/dataset" element={<Dataset />} />
          <Route path="/tests" element={<Tests />} />
          <Route path="/history" element={<History />} />
          <Route path="/config" element={<Config />} />
          <Route path="/admin/users" element={<AdminUsers />} />
          <Route path="/admin/invite-codes" element={<AdminInviteCodes />} />
          <Route path="/admin/credits" element={<AdminCredits />} />
          <Route path="/admin/credits/settings" element={<AdminCreditsSettings />} />
          <Route path="/traces/:traceId" element={<TraceDetail />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster />
    </HashRouter>
  );
}

export default App;
