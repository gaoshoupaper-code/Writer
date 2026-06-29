"use client";

/**
 * 新建测试发起页（/tests/new）。
 *
 * 左侧：数据集卡片列表（点卡片原地展开 demand.md 全文预览），选中高亮。
 * 右侧：Agent 版本单选（working + 快照列表）。
 * 底部：发起测试 → 跳 /tests。
 *
 * 设计依据：.claude/md/20260628_144027_进化端手动测试入口_设计.md（D-Q6/D15）
 */
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  fetchCases,
  fetchCaseDetail,
  fetchTestAgents,
  startTest,
} from "@/lib/tests-api";
import type { CaseSummary, CaseDetail, TestAgent, VersionType } from "@/lib/tests-api";

export default function NewTestPage() {
  const router = useRouter();
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [agents, setAgents] = useState<TestAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 选中的数据集
  const [selectedCase, setSelectedCase] = useState<string | null>(null);
  // 展开预览的 case（点卡片切换）
  const [expandedCase, setExpandedCase] = useState<string | null>(null);
  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 选中的 Agent 版本
  const [selectedAgent, setSelectedAgent] = useState<{
    type: VersionType;
    version: number | null;
  } | null>(null);

  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [cs, ag] = await Promise.all([fetchCases(), fetchTestAgents()]);
      setCases(cs);
      setAgents(ag);
      // 默认选中 working
      if (ag.length > 0 && ag[0].type === "working") {
        setSelectedAgent({ type: "working", version: null });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggleCase = async (caseId: string) => {
    if (expandedCase === caseId) {
      setExpandedCase(null);
      setCaseDetail(null);
      return;
    }
    setSelectedCase(caseId);
    setExpandedCase(caseId);
    setCaseDetail(null);
    setDetailLoading(true);
    try {
      const detail = await fetchCaseDetail(caseId);
      setCaseDetail(detail);
    } catch {
      // 预览失败不阻断选择
    } finally {
      setDetailLoading(false);
    }
  };

  const selectAgent = (agent: TestAgent) => {
    setSelectedAgent({
      type: agent.type,
      version: agent.version ?? null,
    });
  };

  const handleSubmit = async () => {
    if (!selectedCase || !selectedAgent) return;
    setSubmitting(true);
    setError(null);
    try {
      const resp = await startTest({
        case_id: selectedCase,
        version_type: selectedAgent.type,
        version_id: selectedAgent.version,
      });
      // 跳到测试详情页看实时执行
      router.push(`/tests?id=${encodeURIComponent(resp.test_id)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "发起失败");
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="text-dim" style={{ padding: 48, textAlign: "center" }}>
        加载中…
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 className="page-title">新建测试</h1>
        <p className="page-subtitle">
          选择数据集与 Agent 版本，发起一次单次测试运行
        </p>
      </div>

      {error && <div className="error-box">{error}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 24 }}>
        {/* 左侧：数据集卡片列表 */}
        <div>
          <div className="section-title" style={{ marginBottom: 12 }}>
            数据集
          </div>
          {cases.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">∅</div>
              <h3>暂无数据集</h3>
              <p>请在 evolution/data/evalset/ 下添加 case</p>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {cases.map((c) => {
                const isSelected = selectedCase === c.case_id;
                const isExpanded = expandedCase === c.case_id;
                return (
                  <div
                    key={c.case_id}
                    className="card"
                    style={{
                      padding: 0,
                      borderColor: isSelected
                        ? "var(--accent)"
                        : "var(--border)",
                      cursor: "pointer",
                    }}
                    onClick={() => toggleCase(c.case_id)}
                  >
                    <div
                      style={{
                        padding: "14px 18px",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 600, fontSize: 14 }}>
                          {c.title}
                        </div>
                        <div
                          className="text-mute mono"
                          style={{ fontSize: 11, marginTop: 2 }}
                        >
                          {c.case_id}
                        </div>
                      </div>
                      <span className="text-dim" style={{ fontSize: 12 }}>
                        {isExpanded ? "▼" : "▶"}
                      </span>
                    </div>
                    {isExpanded && (
                      <div
                        style={{
                          borderTop: "1px solid var(--border-soft)",
                          padding: "14px 18px",
                          maxHeight: 360,
                          overflow: "auto",
                        }}
                      >
                        {detailLoading ? (
                          <div className="text-dim" style={{ fontSize: 12 }}>
                            加载 demand.md…
                          </div>
                        ) : caseDetail ? (
                          <pre
                            className="mono"
                            style={{
                              fontSize: 11,
                              lineHeight: 1.6,
                              margin: 0,
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                              color: "var(--text-dim)",
                            }}
                          >
                            {caseDetail.demand_md}
                          </pre>
                        ) : (
                          <div className="text-mute" style={{ fontSize: 12 }}>
                            内容加载失败
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 右侧：Agent 版本选择 */}
        <div>
          <div className="section-title" style={{ marginBottom: 12 }}>
            Agent 版本
          </div>
          {agents.length === 0 ? (
            <div className="card-flat" style={{ padding: 20 }}>
              <span className="text-mute" style={{ fontSize: 12 }}>
                暂无可用版本
              </span>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {agents.map((a, i) => {
                const isSelected =
                  selectedAgent?.type === a.type &&
                  selectedAgent?.version === (a.version ?? null);
                return (
                  <div
                    key={`${a.type}-${a.version ?? "w"}-${i}`}
                    className="card"
                    style={{
                      padding: "12px 16px",
                      cursor: "pointer",
                      borderColor: isSelected
                        ? "var(--accent)"
                        : "var(--border)",
                      background: isSelected
                        ? "var(--accent-soft)"
                        : "var(--surface)",
                    }}
                    onClick={() => selectAgent(a)}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                      }}
                    >
                      <span style={{ fontWeight: 600, fontSize: 13 }}>
                        {a.label}
                      </span>
                      {isSelected && (
                        <span
                          style={{ color: "var(--accent)", fontSize: 14 }}
                        >
                          ✓
                        </span>
                      )}
                    </div>
                    {a.change_summary && (
                      <div
                        className="text-mute"
                        style={{ fontSize: 11, marginTop: 4 }}
                      >
                        {a.change_summary}
                      </div>
                    )}
                    {a.source_commit && (
                      <div
                        className="text-mute mono"
                        style={{ fontSize: 10, marginTop: 2 }}
                      >
                        commit: {a.source_commit}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* 底部操作栏 */}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: 12,
          marginTop: 28,
          paddingTop: 20,
          borderTop: "1px solid var(--border-soft)",
        }}
      >
        <button
          className="btn-ghost"
          onClick={() => router.push("/tests")}
          disabled={submitting}
        >
          取消
        </button>
        <button
          className="btn-primary"
          onClick={handleSubmit}
          disabled={!selectedCase || !selectedAgent || submitting}
        >
          {submitting ? "发起中…" : "发起测试"}
        </button>
      </div>
    </div>
  );
}
