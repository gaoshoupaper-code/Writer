"use client";

/**
 * 评估功能（/evaluation）—— 独立评估 Agent（三功能解耦，决策 S1/S7）。
 *
 * 列表+详情合一页（同 /tests 模式）：
 *   - 无 ?id= → 评估记录列表 + 启动评估入口（选 trace）
 *   - ?id=xxx → 评估详情（SSE 实时进度 + scores/findings/report_md）
 *
 * 评估 Agent 对一条 trace 做诊断（评分+问题清单+证据），产出供进化消费。
 */
import { Suspense } from "react";
import { EvaluationList } from "./EvaluationList";
import { EvaluationDetail } from "./EvaluationDetail";

export default function EvaluationPage() {
  return (
    <Suspense
      fallback={
        <div className="text-dim" style={{ padding: 48 }}>
          加载中…
        </div>
      }
    >
      <EvaluationInner />
    </Suspense>
  );
}

import { useSearchParams } from "next/navigation";

function EvaluationInner() {
  const searchParams = useSearchParams();
  const evalId = searchParams.get("id");
  return evalId ? <EvaluationDetail evalId={evalId} /> : <EvaluationList />;
}
