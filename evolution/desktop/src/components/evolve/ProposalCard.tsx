import type { EvolvePoint } from "@/lib/api";

/**
 * 进化点 propose 卡片（决策 Y/T）。
 *
 * 在对话消息内联渲染 Agent 提出的进化点——target / problem / options[]
 * / recommendation / note + 当前状态徽章 + 用户的选择（如有）。
 *
 * 与浮窗（PointsDrawer）的关系：浮窗是清单视图（镜子），本组件是消息内
 * 的详细卡片。两边通过 point.id 双向高亮联动（决策 N）。
 *
 * 纯展示——所有操作（accept/reject/编辑）走对话框，本组件无交互按钮
 * （决策 M：浮窗纯展示，操作走对话；同理卡片也纯展示）。
 */
interface Props {
  point: EvolvePoint;
  highlighted?: boolean; // 双向高亮联动（决策 N）
}

const STATUS_META: Record<
  EvolvePoint["status"],
  { icon: string; label: string; tone: string }
> = {
  proposed: { icon: "○", label: "讨论中", tone: "proposed" },
  accepted: { icon: "✓", label: "已采纳", tone: "accepted" },
  rejected: { icon: "✗", label: "已否决", tone: "rejected" },
};

export default function ProposalCard({ point, highlighted }: Props) {
  const meta = STATUS_META[point.status];
  const chosen = point.status === "accepted" && point.chosen_option !== null
    ? point.options[point.chosen_option ?? 0]
    : null;

  return (
    <div className={`proposal-card status-${meta.tone}${highlighted ? " proposal-highlighted" : ""}`}
         data-point-id={point.id}>
      <header className="proposal-header">
        <span className="proposal-seq">#{point.seq}</span>
        <code className="proposal-target">{point.target}</code>
        <span className={`proposal-status status-${meta.tone}`}>
          <span className="status-icon">{meta.icon}</span>
          {meta.label}
        </span>
      </header>

      <section className="proposal-section">
        <h4 className="section-label">为什么改</h4>
        <p className="section-body">{point.problem}</p>
      </section>

      <section className="proposal-section">
        <h4 className="section-label">备选方案</h4>
        <ol className="options-list">
          {point.options.map((opt, idx) => {
            const isChosen = point.chosen_option === idx;
            return (
              <li
                key={idx}
                className={`option-item${isChosen ? " option-chosen" : ""}`}
              >
                <div className="option-head">
                  <span className="option-index">{String.fromCharCode(65 + idx)}</span>
                  <span className="option-desc">{opt.description}</span>
                  {isChosen && <span className="chosen-tag">用户选择</span>}
                </div>
                <div className="option-meta">
                  {opt.pros.length > 0 && (
                    <span className="meta-pros">优点：{opt.pros.join("；")}</span>
                  )}
                  {opt.cons.length > 0 && (
                    <span className="meta-cons">缺点：{opt.cons.join("；")}</span>
                  )}
                  {opt.expected_impact && (
                    <span className="meta-impact">预期：{opt.expected_impact}</span>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      </section>

      {point.recommendation && (
        <section className="proposal-section">
          <h4 className="section-label">Agent 推荐</h4>
          <p className="section-body recommendation-body">{point.recommendation}</p>
        </section>
      )}

      {chosen && (
        <footer className="proposal-chosen">
          <span className="chosen-label">采纳方案：</span>
          <span className="chosen-desc">{chosen.description}</span>
          {point.user_note && <span className="chosen-note">（{point.user_note}）</span>}
        </footer>
      )}
    </div>
  );
}
