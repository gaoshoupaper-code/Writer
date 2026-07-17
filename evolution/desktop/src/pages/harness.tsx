import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  getSnapshots,
  getElements,
  getMemoryElements,
  getVersionDetail,
  type Snapshot,
  type ElementsView,
  type MemoryElementView,
  type VersionDetail,
  type AgentDiff,
} from "@/lib/api";
import { UpgradeOverview } from "@/components/harness/UpgradeOverview";
import { MemorySubsystemCard } from "@/components/harness/MemorySubsystemCard";
import { PromptTab } from "@/components/harness/PromptTab";
import { SkillsTab } from "@/components/harness/SkillsTab";
import { MiddlewareTab } from "@/components/harness/MiddlewareTab";

/**
 * Agent 要素透视页（重构版）。
 *
 * 选一个版本快照 → 一眼看懂两件事：
 * 1. 这个版本的 Agent 怎么搭的（要素按类型分 Tab：Prompt / Skills / Middleware 泳道图）
 * 2. 这个版本相比父版本改了什么（顶部升级总览条 + 各 Tab 内 diff 高亮）
 *
 * 数据流：并行调 getElements（要素）+ getVersionDetail（升级 diff），version 切换时重拉。
 */
export default function HarnessPage() {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [elements, setElements] = useState<ElementsView | null>(null);
  const [memoryElements, setMemoryElements] = useState<MemoryElementView[] | null>(null);
  const [versionDetail, setVersionDetail] = useState<VersionDetail | null>(null);
  const [loading, setLoading] = useState(true);

  // 拉取版本列表（仅首次）
  const refresh = useCallback(async () => {
    try {
      const snaps = await getSnapshots();
      setSnapshots(snaps);
      if (snaps.length > 0 && selectedVersion === null) {
        const prod = snaps.find((s) => s.status === "production") ?? snaps[0];
        setSelectedVersion(prod.version);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取版本列表失败");
    } finally {
      setLoading(false);
    }
  }, [selectedVersion]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 版本切换：并行拉要素 + 记忆要素 + 升级详情
  useEffect(() => {
    if (selectedVersion == null) return;
    setElements(null);
    setMemoryElements(null);
    setVersionDetail(null);

    Promise.all([
      getElements(selectedVersion).catch((err) => {
        toast.error(err instanceof Error ? err.message : "读取 Agent 要素失败");
        return null;
      }),
      getMemoryElements(selectedVersion).catch((err) => {
        // 记忆要素失败不阻断主要素展示（老版本无此接口/无 NWM）
        console.warn("读取记忆要素失败", err);
        return null;
      }),
      getVersionDetail(selectedVersion).catch((err) => {
        // 版本详情失败不阻断要素展示（diff 高亮不可用而已）
        console.warn("读取版本详情失败", err);
        return null;
      }),
    ]).then(([els, memEls, detail]) => {
      setElements(els);
      setMemoryElements(memEls?.elements ?? []);
      setVersionDetail(detail);
    });
  }, [selectedVersion]);

  // 构建 diffs 查找表：agent 名 → AgentDiff
  const diffs = (() => {
    if (!versionDetail?.changes?.agents) return null;
    const map = new Map<string, AgentDiff>();
    for (const { agent, diff } of versionDetail.changes.agents) {
      map.set(agent, diff);
    }
    return map;
  })();

  const isBootstrap = versionDetail
    ? versionDetail.is_bootstrap || versionDetail.parent_version == null
    : false;

  if (loading) return <div className="page-loading">加载版本列表…</div>;

  return (
    <div className="harness-page">
      <header className="page-header">
        <h1>Agent 要素</h1>
        <p className="page-desc">
          透视各版本 Agent 的内部结构，标注版本升级与 Middleware 生命周期
        </p>
      </header>

      {/* 版本选择 */}
      <div className="harness-version-bar">
        <label>选择版本：</label>
        <select
          className="evolve-select"
          value={selectedVersion ?? ""}
          onChange={(e) => setSelectedVersion(Number(e.target.value))}
        >
          {snapshots.map((s) => (
            <option key={s.version} value={s.version}>
              v{s.version} {s.status === "production" ? "（生产）" : ""} —{" "}
              {s.change_summary?.slice(0, 40) || "无说明"}
            </option>
          ))}
        </select>
      </div>

      {/* 升级总览条 */}
      <UpgradeOverview
        changes={versionDetail?.changes ?? null}
        isBootstrap={isBootstrap}
      />

      {/* 记忆子系统顶部聚焦视图（NWM 6 要素，独立于按 agent 分组的 Tab） */}
      {memoryElements && <MemorySubsystemCard elements={memoryElements} />}

      {/* 要素三 Tab */}
      {elements ? (
        <Tabs defaultValue="prompt" className="harness-tabs">
          <TabsList>
            <TabsTrigger value="prompt">Prompt</TabsTrigger>
            <TabsTrigger value="skills">Skills</TabsTrigger>
            <TabsTrigger value="middleware">Middleware</TabsTrigger>
          </TabsList>
          <TabsContent value="prompt">
            <PromptTab agents={elements.agents} diffs={diffs} />
          </TabsContent>
          <TabsContent value="skills">
            <SkillsTab agents={elements.agents} diffs={diffs} />
          </TabsContent>
          <TabsContent value="middleware">
            <MiddlewareTab
              agents={elements.agents}
              diffs={diffs}
              hasSource={elements.has_source}
            />
          </TabsContent>
        </Tabs>
      ) : (
        <div className="page-loading">加载 Agent 要素…</div>
      )}
    </div>
  );
}
