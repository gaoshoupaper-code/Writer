import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import GoldenTab from "@/components/dataset/GoldenTab";
import GrowingTab from "@/components/dataset/GrowingTab";
import ReviewTab from "@/components/dataset/ReviewTab";

/**
 * 数据集页（设计文档 20260709_160000）。
 *
 * 三 Tab 展示完整数据闭环：
 *   Golden（冻结基准）→ Growing（探索集）→ 待标注（promote 闸门）
 *
 * Tab 容器只负责切换，每 Tab 子组件自管数据获取与刷新（SD2）。
 * Radix Tabs 默认切走卸载（SD6）：ReviewTab 不看时停止轮询。
 */
export default function DatasetPage() {
  return (
    <div className="dataset-page">
      <header className="page-header">
        <h1>数据集</h1>
        <p className="page-desc">
          评估集两层结构：Golden（冻结基准，benchmark 用）· Growing（探索集，生产 promote 入库）· 待标注（promote 闸门队列）
        </p>
      </header>

      <Tabs defaultValue="golden">
        <TabsList>
          <TabsTrigger value="golden">Golden</TabsTrigger>
          <TabsTrigger value="growing">Growing</TabsTrigger>
          <TabsTrigger value="review">待标注</TabsTrigger>
        </TabsList>
        <TabsContent value="golden">
          <GoldenTab />
        </TabsContent>
        <TabsContent value="growing">
          <GrowingTab />
        </TabsContent>
        <TabsContent value="review">
          <ReviewTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
