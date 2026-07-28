import { useEffect, useState } from "react";
import { Eye, EyeOff, FileText, LoaderCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  getArtifactRevisionContent,
  getTraceArtifactRevisions,
} from "@/lib/api";
import type { ArtifactRevision } from "@/lib/types";

type ArtifactsPanelProps = {
  traceId: string;
  canReadContent: boolean;
};

type RevisionContentState =
  | { status: "loading" }
  | { status: "open"; content: unknown }
  | { status: "closed"; content: unknown }
  | { status: "error"; message: string };

export function ArtifactsPanel({ traceId, canReadContent }: ArtifactsPanelProps) {
  const [revisions, setRevisions] = useState<ArtifactRevision[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [contents, setContents] = useState<Record<string, RevisionContentState>>({});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContents({});

    getTraceArtifactRevisions(traceId)
      .then((response) => {
        if (!cancelled) setRevisions(response.items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setRevisions([]);
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [traceId]);

  async function toggleContent(revisionId: string) {
    const current = contents[revisionId];
    if (current?.status === "open") {
      setContents((states) => ({
        ...states,
        [revisionId]: { status: "closed", content: current.content },
      }));
      return;
    }
    if (current?.status === "closed") {
      setContents((states) => ({
        ...states,
        [revisionId]: { status: "open", content: current.content },
      }));
      return;
    }

    setContents((states) => ({ ...states, [revisionId]: { status: "loading" } }));
    try {
      const response = await getArtifactRevisionContent(revisionId);
      setContents((states) => ({
        ...states,
        [revisionId]: { status: "open", content: response.content },
      }));
    } catch (reason) {
      setContents((states) => ({
        ...states,
        [revisionId]: {
          status: "error",
          message: reason instanceof Error ? reason.message : String(reason),
        },
      }));
    }
  }

  if (loading) return <div className="artifacts-loading">加载制品版本...</div>;
  if (error) return <div className="artifacts-error">读取制品版本失败：{error}</div>;
  if (revisions.length === 0) {
    return <div className="artifacts-empty">该 Trace 没有生成可跨运行复查的制品版本。</div>;
  }

  return (
    <div className="artifacts-panel">
      <div className="artifact-revision-list">
        {revisions.map((revision) => {
          const contentState = contents[revision.artifact_revision_id];
          const expired = isExpired(revision.expires_at);
          const isOpen = contentState?.status === "open";
          return (
            <article className="artifact-revision" key={revision.artifact_revision_id}>
              <header className="artifact-revision-header">
                <div className="artifact-revision-title">
                  <FileText size={15} aria-hidden="true" />
                  <div>
                    <h3>{revision.logical_key}</h3>
                    <span>{artifactTypeLabel(revision.artifact_type)}</span>
                  </div>
                </div>
                {canReadContent && (
                  <button
                    className="artifact-content-toggle"
                    type="button"
                    disabled={expired || contentState?.status === "loading"}
                    onClick={() => toggleContent(revision.artifact_revision_id)}
                    title={expired ? "正文已过期" : isOpen ? "收起正文" : "查看正文"}
                    aria-label={expired ? "正文已过期" : isOpen ? "收起正文" : "查看正文"}
                  >
                    {contentState?.status === "loading" ? (
                      <LoaderCircle className="artifact-content-spinner" size={15} />
                    ) : isOpen ? (
                      <EyeOff size={15} />
                    ) : (
                      <Eye size={15} />
                    )}
                  </button>
                )}
              </header>

              <dl className="artifact-revision-meta">
                <Meta label="Revision" value={revision.artifact_revision_id} />
                <Meta label="Content hash" value={revision.content_hash} />
                <Meta label="Parent" value={revision.parent_revision_id ?? "根版本"} />
                <Meta label="Producer event" value={revision.producer_event_id ?? "unknown"} />
                <Meta label="Harness" value={revision.harness_version ?? "unknown"} />
                <Meta label="Size" value={formatBytes(revision.size_bytes)} />
                <Meta label="Created" value={formatDateTime(revision.created_at)} />
                <Meta
                  label="Retention"
                  value={expired ? "正文已过期" : formatDateTime(revision.expires_at)}
                />
              </dl>

              {!canReadContent && (
                <div className="artifact-content-restricted">完整正文仅超级管理员可按需查看。</div>
              )}
              {contentState?.status === "error" && (
                <div className="artifacts-error">读取正文失败：{contentState.message}</div>
              )}
              {contentState?.status === "open" && (
                <RevisionContent content={contentState.content} />
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}

function RevisionContent({ content }: { content: unknown }) {
  if (typeof content === "string") {
    return (
      <div className="artifact-revision-content prose-doc">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    );
  }
  return <pre className="artifact-revision-content">{JSON.stringify(content, null, 2)}</pre>;
}

function artifactTypeLabel(value: string): string {
  const labels: Record<string, string> = {
    draft: "写作稿件",
    evidence_dossier: "证据卷宗",
    evaluation_dossier: "评估卷宗",
    candidate_config: "候选配置",
    release: "发布版本",
  };
  return labels[value] ?? value;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDateTime(value: string | null): string {
  if (!value) return "永久保留";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function isExpired(value: string | null): boolean {
  if (!value) return false;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) || timestamp <= Date.now();
}
