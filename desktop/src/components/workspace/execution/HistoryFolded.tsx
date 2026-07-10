/**
 * HistoryFolded —— 历史消息折叠态（只留结果+可展开执行过程摘要）
 *
 * 触发：切到非当前活跃的 message（历史 message）。
 * 历史 message 只显示最终正文 + 可展开"执行过程"折叠区。
 * 思考流/动态文案不重播。
 */
import { useState } from "react";
import type { StageFlow } from "@/lib/stage";
import { getStageDisplayName } from "@/lib/yan-copy";

interface HistoryFoldedProps {
  stageFlow: StageFlow | null;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sec = ms / 1000;
  return sec < 60 ? `${sec.toFixed(0)}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

export function HistoryFolded({ stageFlow }: HistoryFoldedProps) {
  const [expanded, setExpanded] = useState(false);

  if (!stageFlow || stageFlow.stages.length === 0) return null;

  const totalDuration = formatDuration(stageFlow.totalDurationMs);
  const writingStage = stageFlow.stages.find((s) => s.type === "writing");
  const totalWords = writingStage?.subSteps.reduce((sum, s) => sum + (s.wordCount ?? 0), 0) ?? null;

  return (
    <div className="yan-history" data-phase="history">
      <button
        type="button"
        className="yan-history-toggle"
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? "▴ 收起执行过程" : "▾ 查看执行过程"}
      </button>

      {expanded ? (
        <div className="yan-history-detail">
          <div className="yan-history-trail">
            {stageFlow.stages.map((stage, idx) => (
              <span key={stage.id} className="yan-trail-item">
                {idx > 0 ? <span className="yan-trail-sep">→</span> : null}
                <span className={`yan-trail-mark ${stage.status === "completed" ? "completed" : "failed"}`}>
                  {stage.status === "completed" ? "✓" : "✗"}
                </span>
                <span className="yan-trail-label">{getStageDisplayName(stage.type)}</span>
              </span>
            ))}
          </div>
          <div className="yan-history-stats">
            {totalWords != null ? <span>{totalWords.toLocaleString()} 字</span> : null}
            {totalDuration != null ? <span>耗时 {totalDuration}</span> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
