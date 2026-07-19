import type { EvolvePoint } from "@/lib/api";

/**
 * 进化点浮窗（决策 M/N/C）。
 *
 * 右侧固定栏，实时展示当前 session 的进化点清单（镜子，非控制台）：
 *   - 按状态分组（讨论中 / 已采纳 / 已否决）
 *   - 每项点击 → 触发 onPointClick 滚动到讨论位置（双向高亮联动，决策 N）
 *   - 底部拍板按钮（决策 C）：≥1 个 accepted 即可启用
 *   - 预留扩展位（决策 M）：组件结构上预留快捷操作接入点（V1 隐藏）
 *
 * 与对话区联动：hover/scroll 到讨论 → 本组件对应项高亮（由 highlightedPointId 驱动）。
 */
interface Props {
  points: EvolvePoint[];
  acceptedCount: number;
  canFinalize: boolean; // = conversing 状态 && acceptedCount >= 1
  finalizing?: boolean; // 正在落地（按钮变 loading）
  highlightedPointId?: string | null; // 来自对话区 hover/scroll 的高亮
  onPointClick?: (pointId: string) => void; // 点击浮窗项 → 对话区滚动
  onFinalize?: () => void;
}

const STATUS_GROUPS: Array<{
  status: EvolvePoint["status"];
  title: string;
  emptyHint: string;
}> = [
  { status: "proposed", title: "讨论中", emptyHint: "暂无讨论中的进化点" },
  { status: "accepted", title: "已采纳", emptyHint: "至少采纳 1 个才能拍板" },
  { status: "rejected", title: "已否决", emptyHint: "无" },
];

const STATUS_ICON: Record<EvolvePoint["status"], string> = {
  proposed: "○",
  accepted: "✓",
  rejected: "✗",
};

export default function PointsDrawer({
  points,
  acceptedCount,
  canFinalize,
  finalizing,
  highlightedPointId,
  onPointClick,
  onFinalize,
}: Props) {
  const byStatus = (status: EvolvePoint["status"]) =>
    points.filter((p) => p.status === status);

  return (
    <aside className="evolve-drawer" aria-label="进化点清单">
      <header className="drawer-header">
        <h3 className="drawer-title">进化点</h3>
        <span className="drawer-count">{points.length}</span>
      </header>

      <div className="drawer-body">
        {points.length === 0 ? (
          <div className="drawer-empty">
            <div className="empty-glyph">🌱</div>
            <p className="empty-text">
              还没有提出进化点。
              <br />
              和 Agent 讨论起来吧。
            </p>
          </div>
        ) : (
          STATUS_GROUPS.map((group) => {
            const items = byStatus(group.status);
            if (items.length === 0) return null;
            return (
              <section key={group.status} className="drawer-group">
                <h4 className="group-title">
                  <span className="group-icon">{STATUS_ICON[group.status]}</span>
                  {group.title}
                  <span className="group-count">{items.length}</span>
                </h4>
                <ul className="group-list">
                  {items.map((p) => (
                    <li
                      key={p.id}
                      className={`point-item status-${p.status}${
                        highlightedPointId === p.id ? " point-highlighted" : ""
                      }`}
                      data-point-id={p.id}
                      {...(onPointClick
                        ? { onClick: () => onPointClick(p.id) }
                        : {})}
                    >
                      <div className="point-head">
                        <span className="point-seq">#{p.seq}</span>
                        <code className="point-target">{p.target}</code>
                      </div>
                      <p className="point-problem">{p.problem}</p>
                      {p.status === "accepted" && p.chosen_option !== null && (
                        <p className="point-chosen">
                          → {p.options[p.chosen_option ?? 0]?.description}
                        </p>
                      )}
                      {/* 预留扩展位（决策 M）：未来可在此加 accept/reject 快捷按钮 */}
                    </li>
                  ))}
                </ul>
              </section>
            );
          })
        )}
      </div>

      <footer className="drawer-footer">
        <div className="finalize-status">
          已采纳 <strong>{acceptedCount}</strong> 个进化点
        </div>
        <button
          type="button"
          className="finalize-btn"
          disabled={!canFinalize || finalizing}
          {...(onFinalize ? { onClick: onFinalize } : {})}
        >
          {finalizing ? "正在落地…" : "确认全部进化点，开始落地"}
        </button>
        {!canFinalize && (
          <p className="finalize-hint">
            {acceptedCount === 0
              ? "至少采纳 1 个进化点才能拍板"
              : "只有对话阶段可以拍板"}
          </p>
        )}
      </footer>
    </aside>
  );
}
