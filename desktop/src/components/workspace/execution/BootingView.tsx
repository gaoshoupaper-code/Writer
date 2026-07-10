/**
 * BootingView —— 黑屏期·优雅骨架+呼吸微动画
 *
 * 触发：用户按发送的瞬间，在首个 trace_event/tool_call 到达前。
 * 即时出现（不等后端），消除"发送后空荡"的焦虑。
 *
 * 视觉：小衍头像点(◎) + 准备中文案 + 三点呼吸波纹动画（非 spinner）。
 * 退出：首个执行事件到达 → 淡出 → 切 thinking/writing。
 *
 * T18 记忆感：如果当前会话已有历史交互（hasHistory），
 * 文案从 MEMORY_COPY 取（"好的，接着上次的故事..."），否则用 BOOTING_COPY。
 */
import { useEffect, useState } from "react";
import { BOOTING_COPY, MEMORY_COPY, pickRandom } from "@/lib/yan-copy";
import { useExecutionStore } from "@/stores/execution";

export function BootingView() {
  const hasHistory = useExecutionStore((s) => s.hasHistory);
  const pool = hasHistory ? MEMORY_COPY : BOOTING_COPY;

  // 文案轮播：每 3s 换一条，让用户感觉"活着"
  const [copyIdx, setCopyIdx] = useState(() => Math.floor(Math.random() * pool.length));

  useEffect(() => {
    const timer = setInterval(() => {
      const next = pickRandom(pool, copyIdx);
      setCopyIdx(next.index);
    }, 3000);
    return () => clearInterval(timer);
  }, [copyIdx, pool]);

  return (
    <div className="yan-booting" data-phase="booting">
      <div className="yan-booting-avatar">◎</div>
      <div className="yan-booting-body">
        <span className="yan-booting-text">{pool[copyIdx]}</span>
        <span className="yan-booting-dots">
          <span className="yan-dot" />
          <span className="yan-dot" />
          <span className="yan-dot" />
        </span>
      </div>
    </div>
  );
}
