"use client";

/**
 * 测试记录列表（/tests 无 ?id= 时）。
 *
 * 状态 tab（全部/进行中/已完成/失败）+ created_at 倒序分页。
 * 点击任意行 → 跳测试详情（/tests?id=xxx）看实时执行。
 * failed 行有重试按钮。
 * running/pending 行定时轮询刷新状态（4s）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { deleteTest, fetchTests, retryTest, stopTest } from "@/lib/tests-api";
import type { TestRecord, TestStatus } from "@/lib/tests-api";

const STATUS_TABS: { key: string; label: string }[] = [
  { key: "全部", label: "全部" },
  { key: "running", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "failed", label: "失败" },
];

const PAGE_SIZE = 20;

export function TestsList() {
  const [tests, setTests] = useState<TestRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [tab, setTab] = useState("全部");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchTests({ status: tab, page, page_size: PAGE_SIZE });
      setTests(data.tests);
      setTotal(data.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [tab, page]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // 有进行中记录时定时轮询（D9）
  const hasRunning = tests.some(
    (t) => t.status === "pending" || t.status === "running",
  );
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (hasRunning) {
      pollRef.current = setInterval(load, 4000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [hasRunning, load]);

  const handleRetry = async (testId: string) => {
    setRetrying(testId);
    try {
      const resp = await retryTest(testId);
      // 跳到新测试详情页看实时执行
      window.location.href = `/tests?id=${encodeURIComponent(resp.test_id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "重试失败");
    } finally {
      setRetrying(null);
    }
  };

  const handleStop = async (testId: string) => {
    if (!confirm("停止该测试？会在当前步骤边界停止（非立即），已生成的部分将保留。")) {
      return;
    }
    setStopping(testId);
    try {
      await stopTest(testId);
      setError(null);
      load(); // 立即刷新一次，轮询会继续跟踪到 cancelled 终态
    } catch (e) {
      setError(e instanceof Error ? e.message : "停止失败");
    } finally {
      setStopping(null);
    }
  };

  const handleDelete = async (testId: string, hasTrace: boolean) => {
    const msg = hasTrace
      ? "删除该测试记录及其关联的 trace 数据？此操作不可撤销。"
      : "删除该测试记录？此操作不可撤销。";
    if (!confirm(msg)) return;
    setDeleting(testId);
    try {
      await deleteTest(testId);
      setError(null);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败");
    } finally {
      setDeleting(null);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          marginBottom: 24,
        }}
      >
        <div>
          <h1 className="page-title">手动测试</h1>
          <p className="page-subtitle">
            选数据集 + Agent 版本 → 跑一次生成 → trace 自动进 trace 系统
          </p>
        </div>
        <Link href="/tests/new" className="btn-primary">
          + 新建测试
        </Link>
      </div>

      {/* 状态 tab */}
      <div className="filter-bar" style={{ marginBottom: 16 }}>
        {STATUS_TABS.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => {
                setTab(t.key);
                setPage(1);
              }}
              className="select-input"
              style={
                active
                  ? {
                      background: "var(--accent-soft)",
                      borderColor: "var(--accent)",
                      color: "var(--accent)",
                    }
                  : undefined
              }
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {error && <div className="error-box">{error}</div>}

      {loading ? (
        <div className="text-dim" style={{ padding: 48, textAlign: "center" }}>
          加载中…
        </div>
      ) : tests.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">∅</div>
          <h3>暂无测试记录</h3>
          <p>点右上角「新建测试」发起第一次手动测试</p>
        </div>
      ) : (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>数据集</th>
                <th>Agent 版本</th>
                <th>状态</th>
                <th>创建时间</th>
                <th style={{ textAlign: "right" }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {tests.map((t) => (
                <tr
                  key={t.test_id}
                  style={{ cursor: "pointer" }}
                  onClick={() => {
                    window.location.href = `/tests?id=${encodeURIComponent(t.test_id)}`;
                  }}
                >
                  <td className="mono" style={{ fontSize: 12 }}>
                    {t.case_id}
                  </td>
                  <td>
                    <VersionLabel type={t.version_type} version={t.version_id} />
                  </td>
                  <td>
                    <StatusBadge status={t.status} />
                    {t.status === "failed" && t.error && (
                      <span
                        className="text-mute mono"
                        style={{ fontSize: 10, marginLeft: 8 }}
                        title={t.error}
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td className="text-dim mono" style={{ fontSize: 11 }}>
                    {formatTime(t.created_at)}
                  </td>
                  <td
                    style={{ textAlign: "right" }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {/* 操作：运行中→停止；终态→删除（failed 还能重试） */}
                    {(t.status === "pending" || t.status === "running") && (
                      <button
                        className="btn-ghost"
                        style={{
                          fontSize: 12,
                          padding: "4px 10px",
                          color: "var(--failed)",
                          borderColor: "var(--failed)",
                        }}
                        onClick={() => handleStop(t.test_id)}
                        disabled={stopping === t.test_id}
                      >
                        {stopping === t.test_id ? "停止中…" : "停止"}
                      </button>
                    )}
                    {t.status === "failed" && (
                      <>
                        <button
                          className="btn-ghost"
                          style={{ fontSize: 12, padding: "4px 10px" }}
                          onClick={() => handleRetry(t.test_id)}
                          disabled={retrying === t.test_id}
                        >
                          {retrying === t.test_id ? "重试中…" : "重试"}
                        </button>
                      </>
                    )}
                    {(t.status === "done" ||
                      t.status === "failed" ||
                      t.status === "cancelled") && (
                      <button
                        className="btn-ghost"
                        style={{
                          fontSize: 12,
                          padding: "4px 10px",
                          color: "var(--failed)",
                          borderColor: "var(--failed)",
                          marginLeft: t.status === "failed" ? 6 : 0,
                        }}
                        onClick={() => handleDelete(t.test_id, !!t.trace_id)}
                        disabled={deleting === t.test_id}
                      >
                        {deleting === t.test_id ? "删除中…" : "删除"}
                      </button>
                    )}
                    {(t.status === "pending" || t.status === "running") && (
                      <span
                        className="text-mute"
                        style={{ fontSize: 11, marginLeft: 8 }}
                      >
                        查看 →
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* 分页 */}
          {totalPages > 1 && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 12,
                marginTop: 20,
              }}
            >
              <button
                className="btn-ghost"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                上一页
              </button>
              <span className="text-dim mono" style={{ fontSize: 12 }}>
                {page} / {totalPages}（共 {total} 条）
              </span>
              <button
                className="btn-ghost"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function VersionLabel({
  type,
  version,
}: {
  type: string;
  version: number | null;
}) {
  if (type === "working") {
    return (
      <span className="layer-chip" style={{ fontSize: 11 }}>
        working
      </span>
    );
  }
  return (
    <span
      className="layer-chip"
      style={{ fontSize: 11, background: "var(--accent-soft)", color: "var(--accent)" }}
    >
      v{version}
    </span>
  );
}

function StatusBadge({ status }: { status: TestStatus }) {
  const map: Record<TestStatus, { color: string; label: string }> = {
    pending: { color: "var(--text-mute)", label: "等待" },
    running: { color: "var(--running)", label: "运行中" },
    done: { color: "var(--completed)", label: "完成" },
    failed: { color: "var(--failed)", label: "失败" },
    cancelled: { color: "var(--text-mute)", label: "已停止" },
  };
  const { color, label } = map[status];
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
    });
  } catch {
    return iso;
  }
}
