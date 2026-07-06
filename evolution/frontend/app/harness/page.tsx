"use client";

/**
 * 执行端 Agent 要素展示（/harness）。
 *
 * 左：版本树（production 置顶钉死 + 其余按号倒序）
 * 右：选中版本的要素 Tab（Prompt / Middleware / Skills / Subagents），
 *     Tab 内按 Agent（meta + 5 subagent）纵向排卡片。
 *
 * 版本状态进 URL（?v=N，可分享/刷新保持）；要素 Tab 用组件 state。
 * 设计依据：20260706_150000_Agent要素展示页_设计.md（D1-D7）。
 */
import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { fetchElements, fetchProductionSnapshot, fetchSnapshots } from "@/lib/harness-api";
import type { ElementsView, SnapshotListItem } from "@/lib/harness-types";
import { VersionTree } from "@/components/harness/VersionTree";
import { ElementTabs, type ElementTab } from "@/components/harness/ElementTabs";

export default function HarnessPage() {
  return (
    <Suspense fallback={<div className="text-dim" style={{ padding: 48 }}>加载中…</div>}>
      <HarnessInner />
    </Suspense>
  );
}

function HarnessInner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [snapshots, setSnapshots] = useState<SnapshotListItem[]>([]);
  const [productionVersion, setProductionVersion] = useState<number | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [elements, setElements] = useState<ElementsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<ElementTab>("prompt");

  // 首次加载：拉版本列表 + production
  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [snaps, prod] = await Promise.all([
        fetchSnapshots(),
        fetchProductionSnapshot(),
      ]);
      setSnapshots(snaps);
      const prodV = prod?.version ?? snaps[0]?.version ?? null;
      setProductionVersion(prodV);
      // 默认选中：URL ?v=N 优先，否则 production
      const vParam = searchParams.get("v");
      const v = vParam ? Number(vParam) : prodV;
      if (v != null && !Number.isNaN(v)) {
        setSelectedVersion(v);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [searchParams]);

  useEffect(() => {
    loadSnapshots();
  }, [loadSnapshots]);

  // 选中版本变化时拉要素
  useEffect(() => {
    if (selectedVersion == null) {
      setElements(null);
      return;
    }
    fetchElements(selectedVersion)
      .then(setElements)
      .catch((e) => {
        setError(e instanceof Error ? e.message : "加载要素失败");
        setElements(null);
      });
  }, [selectedVersion]);

  // 切版本：更新 state + 同步 URL
  const handleSelect = (v: number) => {
    setSelectedVersion(v);
    const params = new URLSearchParams(searchParams.toString());
    params.set("v", String(v));
    router.replace(`/harness?${params.toString()}`, { scroll: false });
  };

  return (
    <div>
      <h1 className="page-title">Agent 要素</h1>
      <p className="page-subtitle">
        执行端 harness 包 · meta + 5 subagent · Prompt / Middleware / Skills / Subagents
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="harness-layout">
        <VersionTree
          snapshots={snapshots}
          productionVersion={productionVersion}
          selectedVersion={selectedVersion}
          loading={loading}
          onSelect={handleSelect}
        />
        <div className="harness-detail">
          {elements ? (
            <ElementTabs
              view={elements}
              version={selectedVersion ?? 0}
              tab={tab}
              onTabChange={setTab}
            />
          ) : (
            <div className="card" style={{ padding: 40, textAlign: "center" }}>
              <span className="text-dim">
                {loading ? "加载中…" : "从左侧选择一个版本"}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
