/**
 * WritingProgress —— 写作中·字数进度+动态文案轮播+脉动动画
 *
 * 触发：tool_call 的 subagentType === "writing"。
 * 信息丰富化的核心——让用户持续看到进展。
 *
 * 元素：
 * - 动态文案轮播（活泼陪伴，每 6s 换）
 * - 章节进度（第 X / Y 章）
 * - 实时字数
 * - 字数进度条
 * - 阶段缩略链（构思→规划→写作→收尾）
 */
import { useEffect, useState } from "react";
import type { ChatMessage } from "@/lib/types";
import type { StageFlow } from "@/lib/stage";
import { WRITING_COPY, pickRandom, getStageDisplayName } from "@/lib/yan-copy";

interface WritingProgressProps {
  message: ChatMessage;
  stageFlow: StageFlow | null;
}

// 单章目标字数（用于进度条百分比估算，非精确）
const TARGET_WORDS_PER_CHAPTER = 3000;

export function WritingProgress({ message, stageFlow }: WritingProgressProps) {
  // 动态文案轮播
  const [copyIdx, setCopyIdx] = useState(() => Math.floor(Math.random() * WRITING_COPY.length));
  useEffect(() => {
    const timer = setInterval(() => {
      const next = pickRandom(WRITING_COPY, copyIdx);
      setCopyIdx(next.index);
    }, 6000);
    return () => clearInterval(timer);
  }, [copyIdx]);

  // 从 tools 提取当前写作进度
  const writingTool = message.tools?.find(
    (t) => t.subagentType === "writing" && t.status === "running",
  );
  const chapterIndex = writingTool?.chapterIndex ?? null;
  const totalChapters = writingTool?.totalChapters ?? null;
  const wordCount = writingTool?.wordCount ?? null;

  // 字数进度百分比
  const progressPct = wordCount != null
    ? Math.min(100, Math.round((wordCount / TARGET_WORDS_PER_CHAPTER) * 100))
    : null;

  // 阶段缩略链（从 stageFlow 派生）
  const trail = stageFlow?.stages ?? [];
  const currentStageType = trail.find((s) => s.status === "running")?.type ?? "writing";

  return (
    <div className="yan-writing" data-phase="writing">
      {/* 动态文案 */}
      <div className="yan-writing-headline">
        <span className="yan-status-dot" data-status="running" />
        <span className="yan-writing-copy">{WRITING_COPY[copyIdx]}</span>
      </div>

      {/* 章节进度 + 字数 */}
      {(chapterIndex != null || wordCount != null) && (
        <div className="yan-writing-stats">
          {chapterIndex != null && totalChapters != null ? (
            <span className="yan-writing-chapter">
              第 {chapterIndex} / {totalChapters} 章
            </span>
          ) : chapterIndex != null ? (
            <span className="yan-writing-chapter">第 {chapterIndex} 章</span>
          ) : null}
          {wordCount != null ? (
            <span className="yan-writing-words">已写约 {wordCount.toLocaleString()} 字</span>
          ) : null}
        </div>
      )}

      {/* 字数进度条 */}
      {progressPct != null && (
        <div className="yan-writing-progress-bar">
          <div className="yan-writing-progress-fill" style={{ width: `${progressPct}%` }} />
        </div>
      )}

      {/* 阶段缩略链 */}
      {trail.length > 0 && (
        <div className="yan-writing-trail">
          {trail.map((stage, idx) => (
            <span key={stage.id} className="yan-trail-item">
              {idx > 0 ? <span className="yan-trail-sep">→</span> : null}
              <span className={`yan-trail-mark ${stage.status}`}>
                {stage.status === "completed" ? "✓" : stage.status === "running" ? "●" : "○"}
              </span>
              <span className="yan-trail-label">{getStageDisplayName(stage.type)}</span>
            </span>
          ))}
        </div>
      )}

      {/* 如果没有 stageFlow 但有当前阶段名 */}
      {trail.length === 0 && currentStageType ? (
        <div className="yan-writing-trail">
          <span className="yan-trail-item">
            <span className="yan-trail-mark running">●</span>
            <span className="yan-trail-label">{getStageDisplayName(currentStageType)}</span>
          </span>
        </div>
      ) : null}
    </div>
  );
}
