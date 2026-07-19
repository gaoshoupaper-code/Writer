import { useState } from "react";
import BlueprintTab from "@/components/evolve/BlueprintTab";
import WorkbenchTab from "@/components/evolve/WorkbenchTab";

/**
 * 进化页（Phase 4 双 Tab 重写，决策 F/R）。
 *
 * Tab 1「架构蓝图」：只读展示进化 Agent 系统提示词（决策 Q，后端动态返回）
 * Tab 2「进化工作台」：对话式共创（左会话 / 中对话 / 右浮窗）
 *
 * 替代旧的单体草稿页（裸标签 + 残留暗色），对齐 trace-detail 美观度（决策 O）。
 */
type Tab = "blueprint" | "workbench";

export default function EvolvePage() {
  const [tab, setTab] = useState<Tab>("workbench");

  return (
    <div className="evolve-page">
      <nav className="evolve-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          className={`evolve-tab${tab === "workbench" ? " active" : ""}`}
          aria-selected={tab === "workbench"}
          onClick={() => setTab("workbench")}
        >
          🧬 进化工作台
        </button>
        <button
          type="button"
          role="tab"
          className={`evolve-tab${tab === "blueprint" ? " active" : ""}`}
          aria-selected={tab === "blueprint"}
          onClick={() => setTab("blueprint")}
        >
          📜 架构蓝图
        </button>
      </nav>

      <div className="evolve-tab-panel">
        {tab === "workbench" ? <WorkbenchTab /> : <BlueprintTab />}
      </div>
    </div>
  );
}
