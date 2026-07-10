import { useState } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { RefreshCw } from "lucide-react";
import GoldenTab from "@/components/dataset/GoldenTab";
import GrowingTab from "@/components/dataset/GrowingTab";
import ReviewTab from "@/components/dataset/ReviewTab";

/**
 * 数据集页（设计文档 20260709_160000 / 重构 20260710_164000）。
 *
 * 三 Tab 展示完整数据闭环：
 *   Golden（冻结基准，只读）→ Growing（探索集，只读查看）→ 待标注（promote 闸门）
 *
 * Tab 容器只负责切换，每 Tab 子组件自管数据获取与刷新（SD2）。
 * Radix Tabs 默认切走卸载（SD6）：ReviewTab 不看时停止轮询。
 *
 * 刷新机制（重构 2026-07-10）：
 * 页面头部全局刷新按钮 → 递增 refreshSignal → 通过 prop 传给当前激活的 Tab
 * 子组件 useEffect([refreshSignal]) 响应。因 Radix 卸载非激活 Tab，
 * 天然只有当前 Tab 响应刷新。
 */
export default function DatasetPage() {
  const [activeTab, setActiveTab] = useState("golden");
  const [refreshSignal, setRefreshSignal] = useState(0);

  function handleRefresh() {
    setRefreshSignal((n) => n + 1);
  }

  return (
    <div className="dataset-page">
      <header className="page-header">
        <div className="page-header-row">
          <div>
            <h1>数据集</h1>
            <p className="page-desc">
              评估集两层结构：Golden（冻结基准，benchmark 用）· Growing（探索集，生产 promote 入库）· 待标注（promote 闸门队列）
            </p>
          </div>
          <button
            className="action-link refresh-btn"
            onClick={handleRefresh}
            title="刷新当前列表"
          >
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </header>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="golden">Golden</TabsTrigger>
          <TabsTrigger value="growing">Growing</TabsTrigger>
          <TabsTrigger value="review">待标注</TabsTrigger>
        </TabsList>
        <TabsContent value="golden">
          <GoldenTab refreshSignal={refreshSignal} />
        </TabsContent>
        <TabsContent value="growing">
          <GrowingTab refreshSignal={refreshSignal} />
        </TabsContent>
        <TabsContent value="review">
          <ReviewTab refreshSignal={refreshSignal} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
