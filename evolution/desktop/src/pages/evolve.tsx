import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import BlueprintTab from "@/components/evolve/BlueprintTab";
import HistoryTab from "@/components/evolve/HistoryTab";
import WorkbenchTab from "@/components/evolve/WorkbenchTab";
import type { EvolveSession } from "@/lib/api";

/**
 * 进化页（2026-07-20 三 Tab 重写）。
 *
 * Tab 顺序：进化工作台（默认） | 进化历史 | 架构蓝图
 *   - 「进化工作台」中栏启动入口 + 对话区 + 右浮窗（原左侧历史已移到 Tab 2）
 *   - 「进化历史」纯 session 列表，点击 → 跳工作台并选中
 *   - 「架构蓝图」只读展示 Agent 系统提示词
 *
 * URL 是 single source of truth（DD1/DD2）：
 *   /evolve?tab=workbench|history|blueprint&session=xxx
 *   刷新完全恢复、链接可分享、浏览器前进后退自然。
 *   非法 tab 回退 workbench；非法 session 由 WorkbenchTab 兜底（中栏显示启动入口）。
 *
 * 跨 tab 状态传递（DD4）：HistoryTab 点 session → onSelect(session) →
 *   EvolvePage 写 URL（tab=workbench&session=xxx）→ WorkbenchTab 读 initialSessionId
 *   prop → useEffect 自动 selectSession。session 对象经 initialSession 透传，
 *   避免 WorkbenchTab 重复拉取 status。
 */
type Tab = "workbench" | "history" | "blueprint";

const VALID_TABS: ReadonlySet<Tab> = new Set(["workbench", "history", "blueprint"]);

export default function EvolvePage() {
  const [searchParams, setSearchParams] = useSearchParams();

  // tab：非法值兜底 workbench
  const tabParam = searchParams.get("tab");
  const tab: Tab = tabParam && VALID_TABS.has(tabParam as Tab) ? (tabParam as Tab) : "workbench";

  // session：URL 里的 id（选中态由 WorkbenchTab 内部 state 维护，URL 只是触发源）
  const sessionIdFromUrl = searchParams.get("session");

  // initialSession：HistoryTab 点选时透传整个 session 对象（含 status），
  // 让 WorkbenchTab 不必再拉一次详情就能 selectSession。
  // URL 里只有 id——刷新场景下 WorkbenchTab 需自己根据 id 拉详情（见 WorkbenchTab 注释）。
  const [initialSession, setInitialSession] = useState<EvolveSession | null>(null);

  // URL session 变化时清掉缓存的 session 对象——让 WorkbenchTab 用新 id 自己拉详情
  useEffect(() => {
    if (sessionIdFromUrl === null) {
      setInitialSession(null);
    }
  }, [sessionIdFromUrl]);

  const switchTab = useCallback(
    (next: Tab) => {
      setSearchParams(
        (prev) => {
          prev.set("tab", next);
          // 切到非 workbench 的 tab 时清掉 session 参数——避免历史遗留选中态干扰
          if (next !== "workbench") prev.delete("session");
          return prev;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // HistoryTab 点 session：透传整个对象 + 写 URL（触发 WorkbenchTab 切换显示）
  const selectFromHistory = useCallback(
    (session: EvolveSession) => {
      setInitialSession(session);
      setSearchParams(
        (prev) => {
          prev.set("tab", "workbench");
          prev.set("session", session.session_id);
          return prev;
        },
        { replace: false },
      );
    },
    [setSearchParams],
  );

  return (
    <div className="evolve-page">
      <nav className="evolve-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          className={`evolve-tab${tab === "workbench" ? " active" : ""}`}
          aria-selected={tab === "workbench"}
          onClick={() => switchTab("workbench")}
        >
          🧬 进化工作台
        </button>
        <button
          type="button"
          role="tab"
          className={`evolve-tab${tab === "history" ? " active" : ""}`}
          aria-selected={tab === "history"}
          onClick={() => switchTab("history")}
        >
          📚 进化历史
        </button>
        <button
          type="button"
          role="tab"
          className={`evolve-tab${tab === "blueprint" ? " active" : ""}`}
          aria-selected={tab === "blueprint"}
          onClick={() => switchTab("blueprint")}
        >
          📜 架构蓝图
        </button>
      </nav>

      <div className="evolve-tab-panel">
        {tab === "workbench" && (
          <WorkbenchTab
            initialSessionId={sessionIdFromUrl}
            initialSession={initialSession}
          />
        )}
        {tab === "history" && <HistoryTab onSelect={selectFromHistory} />}
        {tab === "blueprint" && <BlueprintTab />}
      </div>
    </div>
  );
}
