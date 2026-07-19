import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { getEvolveSystemPrompt, type EvolveSystemPrompt } from "@/lib/api";

/**
 * 架构蓝图 Tab（决策 F/Q/R）。
 *
 * 只读展示进化 Agent 的 STATIC_BLUEPRINT——7 段全景 + 角色定位 +
 * 能力边界 + 对创作 Agent 的理解。打开进化页即可看，不依赖任何 session。
 * 不可编辑（决策 F），用于建立用户对 Agent 判断的信任。
 */
export default function BlueprintTab() {
  const [data, setData] = useState<EvolveSystemPrompt | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await getEvolveSystemPrompt();
        if (!cancelled) {
          setData(resp);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "加载蓝图失败";
          setError(msg);
          toast.error(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="evolve-blueprint evolve-blueprint-loading">
        <div className="blueprint-skeleton">
          <div className="skeleton-line w1-3" />
          <div className="skeleton-line w2-3" />
          <div className="skeleton-line w1-2" />
        </div>
        <p className="loading-hint">正在加载进化 Agent 架构蓝图…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="evolve-blueprint evolve-blueprint-error">
        <p className="error-text">⚠ {error || "蓝图数据为空"}</p>
      </div>
    );
  }

  return (
    <div className="evolve-blueprint">
      <header className="blueprint-header">
        <div className="blueprint-title-row">
          <h2 className="blueprint-title">进化 Agent 架构蓝图</h2>
          <span className="blueprint-version">v{data.version}</span>
        </div>
        <p className="blueprint-subtitle">
          这是进化 Agent 的认知内核——它脑子里装了什么、能改什么、不能改什么。
          建立对它判断的信任，再决定怎么和它共创。
        </p>
      </header>
      <article className="blueprint-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.blueprint}</ReactMarkdown>
      </article>
    </div>
  );
}
