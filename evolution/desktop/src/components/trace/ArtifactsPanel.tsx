import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  getWorkspaceNovel,
  getWorkspaceStoryline,
  getWorkspaceStorylineGraph,
} from "@/lib/api";
import type {
  NovelChapter,
  StorylineEntry,
  WorkspaceNovelContent,
  WorkspaceStorylineContent,
  WorkspaceStorylineGraphContent,
} from "@/lib/types";

/**
 * Trace 详情页「产物」tab：预览这次 trace 生成的正文 + 人物故事线。
 *
 * 数据源：直调 executor 的 /api/workspaces/{id}/* 接口（session cookie 由
 * Tauri 共享 cookie jar 自动带）。三个请求并行拉取，单个失败不影响其它——
 * 比如写作还没跑到正文阶段，正文区显示空态，故事线区仍能正常展示。
 *
 * 错误态按 HTTP 状态码区分：401=登录态失效、403=非 owner、404=产物尚未生成。
 * 其它错误归为「读取失败」并展示原始信息，方便定位。
 */
type ArtifactsPanelProps = {
  workspaceId: string;
};

// 错误信息（从 apiJson 抛的 Error.message 里反解 HTTP 状态码）。
type ErrorInfo = { httpStatus: number | null; message: string };

// 三类产物各自的加载状态——独立管理，互不阻塞。
type LoadState<T> =
  | { status: "loading" }
  | { status: "ok"; data: T }
  | ({ status: "error" } & ErrorInfo);

/** 把 catch 到的 error 转成 ErrorInfo。apiJson 抛的 Error.message 是响应体或 "HTTP {status}"。 */
function toErrorInfo(err: unknown): ErrorInfo {
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/HTTP (\d+)/);
  return { httpStatus: m ? Number(m[1]) : null, message: msg };
}

/** 根据 HTTP 状态码返回友好的中文错误文案。 */
function errorText({ httpStatus, message }: ErrorInfo): string {
  if (httpStatus === 401) return "登录态已失效，请重新登录后再试。";
  if (httpStatus === 403) return "当前账号不是该 workspace 的所有者，无法预览产物。";
  if (httpStatus === 404) return "尚未生成该产物（对应阶段可能还没跑到）。";
  return `读取失败：${message}`;
}

export function ArtifactsPanel({ workspaceId }: ArtifactsPanelProps) {
  const [novel, setNovel] = useState<LoadState<WorkspaceNovelContent>>({ status: "loading" });
  const [storyline, setStoryline] = useState<LoadState<WorkspaceStorylineContent>>({
    status: "loading",
  });
  const [graph, setGraph] = useState<LoadState<WorkspaceStorylineGraphContent>>({
    status: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    // 三个请求互不依赖：任意一个失败不影响其它展示。
    // 组件卸载（切走 tab）后通过 cancelled 标志丢弃结果，避免 setState on unmounted。
    setNovel({ status: "loading" });
    setStoryline({ status: "loading" });
    setGraph({ status: "loading" });

    getWorkspaceNovel(workspaceId)
      .then((data) => !cancelled && setNovel({ status: "ok", data }))
      .catch((e) => !cancelled && setNovel({ status: "error", ...toErrorInfo(e) }));

    getWorkspaceStoryline(workspaceId)
      .then((data) => !cancelled && setStoryline({ status: "ok", data }))
      .catch((e) => !cancelled && setStoryline({ status: "error", ...toErrorInfo(e) }));

    getWorkspaceStorylineGraph(workspaceId)
      .then((data) => !cancelled && setGraph({ status: "ok", data }))
      .catch((e) => !cancelled && setGraph({ status: "error", ...toErrorInfo(e) }));

    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  return (
    <div className="artifacts-panel">
      {/* 人物故事线区 */}
      <section className="artifacts-section">
        <h3 className="artifacts-section-title">人物故事线</h3>
        <StorylineBlock storyline={storyline} graph={graph} />
      </section>

      {/* 正文区 */}
      <section className="artifacts-section">
        <h3 className="artifacts-section-title">正文</h3>
        <NovelBlock novel={novel} />
      </section>
    </div>
  );
}

/** 故事线索引 + 流程图 + 各线详情。三者独立加载态。 */
function StorylineBlock({
  storyline,
  graph,
}: {
  storyline: LoadState<WorkspaceStorylineContent>;
  graph: LoadState<WorkspaceStorylineGraphContent>;
}) {
  // 两者都还在加载 → 统一 loading 提示，避免出现两行"加载中…"。
  if (storyline.status === "loading" && graph.status === "loading") {
    return <div className="artifacts-loading">加载中…</div>;
  }

  // 两者都成功但都没数据 → 空态（storybuilding 还没跑到）。
  // 任一为 error / loading / 有数据时走下面的分支渲染。
  const slEmpty =
    storyline.status === "ok" &&
    storyline.data.entries.length === 0 &&
    !storyline.data.index_markdown;
  const gEmpty = graph.status === "ok" && !graph.data.markdown;
  if (slEmpty && gEmpty) {
    return <div className="artifacts-empty">本次测试未生成故事线（storybuilding 阶段可能还没跑到）。</div>;
  }

  return (
    <div className="artifacts-subsections">
      {/* 故事线索引 + 各线详情 */}
      {storyline.status === "ok" && <StorylineIndex data={storyline.data} />}
      {storyline.status === "error" && (
        <div className="artifacts-error">{errorText(storyline)}</div>
      )}
      {storyline.status === "loading" && <div className="artifacts-loading">故事线索引加载中…</div>}

      {/* 故事线流程图（markdown 文本） */}
      {graph.status === "ok" && graph.data.markdown && (
        <div className="artifacts-md prose-doc storyline-graph-md">
          <h4 className="artifacts-subsection-title">故事线流程图</h4>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{graph.data.markdown}</ReactMarkdown>
        </div>
      )}
      {graph.status === "error" && (
        <div className="artifacts-error">{errorText(graph)}</div>
      )}
      {graph.status === "loading" && <div className="artifacts-loading">故事线流程图加载中…</div>}
    </div>
  );
}

/** 故事线索引 + 各条故事线详情。空数据时返回 null（由上层 StorylineBlock 统一处理空态）。 */
function StorylineIndex({ data }: { data: WorkspaceStorylineContent }) {
  if (!data.index_markdown && data.entries.length === 0) return null;
  return (
    <div className="artifacts-md prose-doc storyline-index-md">
      {data.index_markdown && (
        <>
          <h4 className="artifacts-subsection-title">故事线索引</h4>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.index_markdown}</ReactMarkdown>
        </>
      )}
      {data.entries.length > 0 && (
        <>
          <h4 className="artifacts-subsection-title">各条故事线</h4>
          {data.entries.map((entry: StorylineEntry) => (
            <div key={entry.filename} className="artifacts-md storyline-entry-md">
              <h5 className="artifacts-entry-title">{entry.title || entry.filename}</h5>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.markdown}</ReactMarkdown>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/** 正文：按章渲染。 */
function NovelBlock({ novel }: { novel: LoadState<WorkspaceNovelContent> }) {
  if (novel.status === "loading") return <div className="artifacts-loading">加载中…</div>;
  if (novel.status === "error") {
    return <div className="artifacts-error">{errorText(novel)}</div>;
  }
  if (novel.data.chapters.length === 0) {
    return <div className="artifacts-empty">本次测试未生成正文（writing 阶段可能还没跑到）。</div>;
  }
  return (
    <div className="artifacts-chapters">
      {novel.data.chapters.map((ch: NovelChapter, idx: number) => (
        <section key={ch.filename} className="artifacts-chapter">
          <header className="artifacts-chapter-header">
            <span className="artifacts-chapter-no">第 {idx + 1} 章</span>
            <h4 className="artifacts-chapter-title">{ch.title || ch.filename}</h4>
          </header>
          <div className="artifacts-md prose-doc">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{ch.markdown}</ReactMarkdown>
          </div>
        </section>
      ))}
    </div>
  );
}
