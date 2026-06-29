"use client";

/**
 * 手动测试（/tests）。
 *
 * 列表+详情合一页（同 /traces 的模式）：
 *   - 无 ?id= → 测试记录列表（状态 tab + 分页，running 行可点进实时详情）
 *   - ?id=xxx → 测试详情（元信息 + trace_id 就绪前等待 + 就绪后 useTraceStream 实时展示）
 *
 * 设计依据：.claude/md/20260628_144027_进化端手动测试入口_设计.md
 */
import { Suspense } from "react";
import { TestsList } from "./TestsList";
import { TestDetail } from "./TestDetail";

export default function TestsPage() {
  return (
    <Suspense
      fallback={
        <div className="text-dim" style={{ padding: 48 }}>
          加载中…
        </div>
      }
    >
      <TestsInner />
    </Suspense>
  );
}

import { useSearchParams } from "next/navigation";

function TestsInner() {
  const searchParams = useSearchParams();
  const testId = searchParams.get("id");
  return testId ? <TestDetail testId={testId} /> : <TestsList />;
}
