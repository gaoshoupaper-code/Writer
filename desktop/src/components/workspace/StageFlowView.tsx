import { useState } from "react";
import type { StageFlow, Stage } from "../../lib/stage";
import { Badge } from "@/components/ui/badge";
import { StageTrail } from "./StageTrail";

const STATUS_LABEL: Record<Stage["status"], string> = {
  running: "进行中",
  completed: "已完成",
  failed: "失败",
};

function statusVariant(status: Stage["status"]): "running" | "completed" | "failed" {
  return status;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sec = ms / 1000;
  return sec < 60 ? `${sec.toFixed(0)}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

/**
 * 阶段流视图：嵌入 assistant message，把 trace 的过程信息（子代理阶段、章节进度、思考摘要）
 * 重新组织进主对话流。最新消息默认展开，历史消息默认折叠（D8）。
 */
export function StageFlowView({ flow, defaultExpanded }: { flow: StageFlow; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [openStage, setOpenStage] = useState<string | null>(null);

  if (flow.stages.length === 0) return null;

  const current = flow.stages.find((s) => s.id === flow.currentStageId) ?? flow.stages[flow.stages.length - 1];
  const completedCount = flow.stages.filter((s) => s.status === "completed").length;
  const totalDuration = formatDuration(flow.totalDurationMs);

  return (
    <div className="stage-flow" data-flow-status={flow.status}>
      {/* 折叠摘要行：running 显示焦点文案，否则显示完成进度 */}
      <button type="button" className="stage-flow-summary" onClick={() => setExpanded((v) => !v)}>
        <span className="stage-flow-status-dot" data-status={flow.status} />
        <span className="stage-flow-summary-text">
          {flow.status === "running"
            ? current?.focusText ?? current?.label ?? "正在执行"
            : `${completedCount}/${flow.stages.length} 阶段完成`}
        </span>
        {totalDuration ? <span className="stage-flow-duration">{totalDuration}</span> : null}
        <span className="stage-flow-caret" aria-hidden>{expanded ? "▾" : "▸"}</span>
      </button>

      {!expanded ? <StageTrail stages={flow.stages} /> : null}

      {expanded ? (
        <ol className="stage-flow-list">
          {flow.stages.map((stage, idx) => {
            const isCurrent = stage.id === flow.currentStageId && stage.status === "running";
            const stageDuration = formatDuration(stage.durationMs);
            return (
              <li
                key={stage.id}
                className={`stage-flow-stage ${stage.status}${isCurrent ? " is-current" : ""}`}
              >
                <div className="stage-flow-stage-header">
                  <span className="stage-flow-stage-index">{idx + 1}</span>
                  <span className="stage-flow-stage-label">{stage.label}</span>
                  <Badge variant={statusVariant(stage.status)} className="text-[10px] py-0 px-1.5">
                    {STATUS_LABEL[stage.status]}
                  </Badge>
                  <span className="stage-flow-stage-meta">
                    {stage.iteration ? `第 ${stage.iteration.current} 轮 · ` : ""}
                    {stage.agentTaskCount} 个子代理任务 · {stage.toolCallCount} 次工具调用
                    {stageDuration ? ` · ${stageDuration}` : ""}
                  </span>
                </div>

                {stage.focusText && stage.status === "running" ? (
                  <div className="stage-flow-focus">{stage.focusText}</div>
                ) : null}

                <ul className="stage-flow-substeps">
                  {stage.subSteps.map((step) => (
                    <li key={step.id} className={`stage-flow-substep ${step.status}`}>
                      <span className="stage-flow-substep-dot" data-status={step.status} />
                      <span className="stage-flow-substep-label">{step.label}</span>
                      {step.wordCount ? <span className="stage-flow-substep-wc">{step.wordCount} 字</span> : null}
                    </li>
                  ))}
                </ul>

                {stage.summary ? (
                  <button
                    type="button"
                    className="stage-flow-summary-toggle"
                    onClick={() => setOpenStage((cur) => (cur === stage.id ? null : stage.id))}
                  >
                    {openStage === stage.id ? "收起思考摘要" : "查看思考摘要"}
                  </button>
                ) : null}
                {openStage === stage.id && stage.summary ? (
                  <p className="stage-flow-stage-summary">{stage.summary}</p>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}
