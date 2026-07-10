/**
 * DeliveryCeremony —— 交付仪式·庆祝+字数/耗时摘要
 *
 * 触发：final 事件 → message.status: completed → phase: delivering。
 * 制造成就感峰值。
 *
 * 元素：
 * - ✨ 交稿语（随机，庆祝调性）
 * - 结果摘要（产出内容/字数/耗时）
 * - 收尾互动语
 */
import { useMemo } from "react";
import type { ChatMessage } from "@/lib/types";
import type { StageFlow } from "@/lib/stage";
import { DELIVERY_COPY, DELIVERY_INTERACTION, pickRandom } from "@/lib/yan-copy";

interface DeliveryCeremonyProps {
  message: ChatMessage;
  stageFlow: StageFlow | null;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sec = ms / 1000;
  return sec < 60 ? `${sec.toFixed(0)}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

export function DeliveryCeremony({ message, stageFlow }: DeliveryCeremonyProps) {
  const deliveryLine = useMemo(() => pickRandom(DELIVERY_COPY).text, []);

  // 从 stageFlow 派生摘要
  const totalDuration = formatDuration(stageFlow?.totalDurationMs);
  const writingStage = stageFlow?.stages.find((s) => s.type === "writing");
  const totalWords = writingStage?.subSteps.reduce((sum, s) => sum + (s.wordCount ?? 0), 0) ?? null;
  const chapterCount = writingStage?.subSteps.length ?? null;

  return (
    <div className="yan-delivery" data-phase="delivering">
      <div className="yan-delivery-header">
        <span className="yan-delivery-sparkle">✨</span>
        <span className="yan-delivery-title">{deliveryLine}</span>
      </div>

      {(totalWords != null || chapterCount != null || totalDuration != null) && (
        <div className="yan-delivery-summary">
          {chapterCount != null && chapterCount > 0 ? (
            <span className="yan-delivery-stat">{chapterCount} 章正文</span>
          ) : null}
          {totalWords != null ? (
            <span className="yan-delivery-stat">{totalWords.toLocaleString()} 字</span>
          ) : null}
          {totalDuration != null ? (
            <span className="yan-delivery-stat">耗时 {totalDuration}</span>
          ) : null}
        </div>
      )}

      <p className="yan-delivery-interaction">{DELIVERY_INTERACTION}</p>
    </div>
  );
}
