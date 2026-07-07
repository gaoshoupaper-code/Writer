import type { Stage } from "../../lib/stage";

// 缩略短名（构建/细纲/正文/辅助），与 STAGE_LABELS 对齐但更紧凑
const TRAIL_SHORT: Record<Stage["type"], string> = {
  storybuilding: "构建",
  "detail-outline": "细纲",
  writing: "正文",
  general: "辅助",
};

function trailMark(status: Stage["status"]): string {
  if (status === "completed") return "✓";
  if (status === "running") return "●";
  return "○";
}

/**
 * 推进轨迹缩略链（D1）：折叠态在 summary 下方渲染 ✓/●/○ + 短名，
 * 一眼看出已完成阶段与当前推进位置。running 项脉动高亮。
 */
export function StageTrail({ stages }: { stages: Stage[] }) {
  if (stages.length === 0) return null;
  return (
    <div className="stage-flow-trail" aria-hidden>
      {stages.map((stage, idx) => (
        <span key={stage.id} className="stage-flow-trail-item">
          {idx > 0 ? <span className="stage-flow-trail-sep">→</span> : null}
          <span className={`stage-flow-trail-mark ${stage.status}`}>{trailMark(stage.status)}</span>
          <span className="stage-flow-trail-name">{TRAIL_SHORT[stage.type]}</span>
        </span>
      ))}
    </div>
  );
}
