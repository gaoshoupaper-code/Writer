"use client";

/**
 * 测试详情（/tests?id=xxx）。
 *
 * 单次手动测试的执行视图：
 *   1. 测试元信息（数据集 / Agent 版本 / 状态 / 时间）
 *   2. trace_id 就绪前：轮询测试记录状态，显示"等待 executor 创建 trace…"
 *   3. trace_id 就绪后：复用 useTraceStream + TracePanel 实时展示节点树时间线
 *      （运行中的 LLM/tool 显示骨架节点 = "看到他正在运行"）
 *
 * 数据源：GET /api/tests/{id} 轮询（2s）+ useTraceStream(traceId) SSE 实时流。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { deleteTest, fetchTestDetail, retryTest, stopTest } from "@/lib/tests-api";
import type { TestRecord } from "@/lib/tests-api";
import { useTraceStream } from "@/hooks/useTraceStream";
import { TracePanel } from "@/components/trace/TracePanel";

export function TestDetail({ testId }: { testId: string }) {
  const router = useRouter();
  const [test, setTest] = useState<TestRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    if (!testId) return;
    try {
      const t = await fetchTestDetail(testId);
      setTest(t);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [testId]);

  useEffect(() => {
    load();
  }, [load]);

  // 测试未终结时定时轮询测试记录（拿 trace_id + 状态）
  const needsPoll =
    test !== null &&
    test.status !== "done" &&
    test.status !== "failed" &&
    test.status !== "cancelled";
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (needsPoll) {
      pollRef.current = setInterval(load, 2000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [needsPoll, load]);

  const handleRetry = async () => {
    if (!test) return;
    setRetrying(true);
    try {
      const resp = await retryTest(test.test_id);
      window.location.href = `/tests?id=${encodeURIComponent(resp.test_id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "重试失败");
      setRetrying(false);
    }
  };

  const handleStop = async () => {
    if (!test) return;
    if (!confirm("停止该测试？会在当前步骤边界停止（非立即），已生成的部分将保留。")) {
      return;
    }
    setStopping(true);
    try {
      await stopTest(test.test_id);
      setError(null);
      // 不立即跳转，轮询会拉到 cancelled 终态；保持 stopping 态直到状态刷新
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "停止失败");
      setStopping(false);
    }
  };

  const handleDelete = async () => {
    if (!test) return;
    const msg = test.trace_id
      ? "删除该测试记录及其关联的 trace 数据？此操作不可撤销。"
      : "删除该测试记录？此操作不可撤销。";
    if (!confirm(msg)) return;
    setDeleting(true);
    try {
      await deleteTest(test.test_id);
      router.push("/tests");
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败");
      setDeleting(false);
    }
  };

  // trace_id 就绪后，用 useTraceStream 实时展示
  const traceId = test?.trace_id ?? null;
  const { detail, isLive, loading: traceLoading, error: traceError } =
    useTraceStream(traceId);

  if (loading) {
    return (
      <div className="text-dim" style={{ padding: 48, textAlign: "center" }}>
        加载中…
      </div>
    );
  }

  if (error && !test) {
    return (
      <div>
        <Link href="/tests" className="cockpit-back mono text-mute">
          ← 测试列表
        </Link>
        <div className="error-box" style={{ marginTop: 16 }}>
          {error}
        </div>
      </div>
    );
  }

  if (!test) return null;

  const showTrace = traceId !== null;
  const waitingForTrace =
    (test.status === "pending" || test.status === "running") && !traceId;

  return (
    <div>
      <Link href="/tests" className="cockpit-back mono text-mute">
        ← 测试列表
      </Link>

      {/* 测试元信息卡 */}
      <div
        className="card"
        style={{
          display: "flex",
          gap: 28,
          alignItems: "center",
          flexWrap: "wrap",
          margin: "12px 0 16px",
          padding: "14px 18px",
        }}
      >
        <MetaField label="测试 ID">
          <span className="mono" style={{ fontSize: 13 }}>
            {test.test_id.slice(0, 16)}
          </span>
        </MetaField>
        <MetaField label="数据集">
          <span className="mono" style={{ fontSize: 13 }}>
            {test.case_id}
          </span>
        </MetaField>
        <MetaField label="Agent 版本">
          {test.version_type === "working" ? (
            <span className="layer-chip" style={{ fontSize: 11 }}>
              working
            </span>
          ) : (
            <span
              className="layer-chip"
              style={{
                fontSize: 11,
                background: "var(--accent-soft)",
                color: "var(--accent)",
              }}
            >
              v{test.version_id}
            </span>
          )}
        </MetaField>
        <MetaField label="状态">
          <TestStatusBadge status={test.status} />
        </MetaField>
        <MetaField label="创建时间">
          <span className="text-dim mono" style={{ fontSize: 11 }}>
            {formatTime(test.created_at)}
          </span>
        </MetaField>
        {traceId && (
          <MetaField label="Trace ID">
            <span className="mono" style={{ fontSize: 11 }}>
              {traceId.slice(0, 20)}
            </span>
          </MetaField>
        )}
        {traceId && (
          <MetaField label="实时">
            {isLive ? (
              <span
                style={{
                  color: "var(--running)",
                  fontSize: 12,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <span
                  className="pulse-dot"
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: "var(--running)",
                  }}
                />
                实时同步
              </span>
            ) : (
              <span className="text-dim" style={{ fontSize: 12 }}>
                历史快照
              </span>
            )}
          </MetaField>
        )}
        {/* 操作区：运行中可停止；终态可删除 */}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          {(test.status === "pending" || test.status === "running") && (
            <button
              className="btn-ghost"
              style={{
                fontSize: 12,
                padding: "4px 12px",
                color: "var(--failed)",
                borderColor: "var(--failed)",
              }}
              onClick={handleStop}
              disabled={stopping}
            >
              {stopping ? "停止中…" : "停止"}
            </button>
          )}
          {(test.status === "done" ||
            test.status === "failed" ||
            test.status === "cancelled") && (
            <button
              className="btn-ghost"
              style={{
                fontSize: 12,
                padding: "4px 12px",
                color: "var(--failed)",
                borderColor: "var(--failed)",
              }}
              onClick={handleDelete}
              disabled={deleting}
            >
              {deleting ? "删除中…" : "删除"}
            </button>
          )}
        </div>
      </div>

      {/* 失败信息 */}
      {test.status === "failed" && test.error && (
        <div className="error-box" style={{ marginBottom: 16 }}>
          <strong>失败原因：</strong> {test.error}
          <button
            className="btn-ghost"
            style={{ marginLeft: 16, fontSize: 12, padding: "4px 10px" }}
            onClick={handleRetry}
            disabled={retrying}
          >
            {retrying ? "重试中…" : "重试"}
          </button>
        </div>
      )}

      {/* 已停止提示 */}
      {test.status === "cancelled" && (
        <div
          className="card"
          style={{
            marginBottom: 16,
            padding: "10px 16px",
            color: "var(--text-mute)",
            fontSize: 13,
          }}
        >
          该测试已被手动停止。已生成的部分内容保留在 trace 中。
        </div>
      )}

      {/* trace 未就绪：等待状态 */}
      {waitingForTrace && (
        <div className="card">
          <div className="empty-state">
            <div
              className="pulse-dot"
              style={{
                width: 14,
                height: 14,
                borderRadius: "50%",
                background: "var(--running)",
                margin: "0 auto 12px",
              }}
            />
            <h3>等待 executor 创建 trace…</h3>
            <p className="text-dim">
              任务已提交（task: {test.task_id?.slice(0, 12) ?? "—"}），executor 正在准备执行环境
            </p>
          </div>
        </div>
      )}

      {/* trace 就绪：实时展示 */}
      {showTrace && (
        <>
          {traceError && !detail ? (
            <div className="error-box">{traceError}</div>
          ) : traceLoading && !detail ? (
            <div className="card">
              <div className="empty-state">
                <h3>加载 trace…</h3>
                <p>正在拉取 trace 详情</p>
              </div>
            </div>
          ) : !detail ? (
            <div className="card">
              <div className="empty-state">
                <h3>trace 数据尚未就绪</h3>
                <p>trace_id 已分配，事件流即将开始</p>
              </div>
            </div>
          ) : (
            <TracePanel
              runs={[detail.run]}
              detail={detail}
              activeTraceId={traceId}
              loading={traceLoading}
              hasActiveThread={true}
              deletingTraceId={""}
              onSelectTrace={() => {}}
              onDeleteTrace={() => {}}
            />
          )}
        </>
      )}
    </div>
  );
}

function MetaField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="field-label" style={{ marginBottom: 4 }}>
        {label}
      </div>
      {children}
    </div>
  );
}

function TestStatusBadge({ status }: { status: string }) {
  const map: Record<string, { color: string; label: string }> = {
    pending: { color: "var(--text-mute)", label: "等待" },
    running: { color: "var(--running)", label: "运行中" },
    done: { color: "var(--completed)", label: "完成" },
    failed: { color: "var(--failed)", label: "失败" },
    cancelled: { color: "var(--text-mute)", label: "已停止" },
  };
  const { color, label } = map[status] ?? {
    color: "var(--text-mute)",
    label: status,
  };
  return (
    <span
      className="status-badge"
      style={{
        color,
        background: `${color}1a`,
        border: `1px solid ${color}40`,
      }}
    >
      {status === "running" && (
        <span
          className="pulse-dot"
          style={{ background: color, width: 6, height: 6 }}
        />
      )}
      {label}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}
