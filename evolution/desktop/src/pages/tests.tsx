import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getTests,
  startTest,
  getTestAgents,
  retryTest,
  stopTest,
  deleteTest,
  getDatasetCases,
  type ManualTest,
  type TestAgentOption,
  type DatasetCase,
} from "@/lib/api";

/**
 * 单次测试页（设计文档：试验台大 tab，独立于核心工作区）。
 *
 * 工作流：
 * 1. 选数据集 case + Agent 版本
 * 2. 启动测试 → 等待 trace 产出
 * 3. 查看结果 / 重试 / 停止 / 删除
 */
export default function TestsPage() {
  const navigate = useNavigate();
  const [tests, setTests] = useState<ManualTest[]>([]);
  const [agents, setAgents] = useState<TestAgentOption[]>([]);
  const [cases, setCases] = useState<DatasetCase[]>([]);
  const [casesLoadError, setCasesLoadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);

  // 新建测试表单
  const [caseId, setCaseId] = useState("");
  const [versionType, setVersionType] = useState("working");
  const [versionId, setVersionId] = useState("");

  const [statusFilter, setStatusFilter] = useState("全部");

  /** 拉取数据集 case 列表，供「新建测试」下拉选择。 */
  const refreshCases = useCallback(async () => {
    try {
      const resp = await getDatasetCases();
      setCases(resp.cases);
      setCasesLoadError(false);
      // 默认选中第一个，避免空提交
      setCaseId((prev) => prev || resp.cases[0]?.case_id || "");
    } catch (err) {
      // 失败不静默：标记错误态让下拉显示"加载失败"，并 toast 提示
      setCases([]);
      setCasesLoadError(true);
      toast.error(err instanceof Error ? err.message : "数据集 case 加载失败");
    }
  }, []);

  useEffect(() => {
    refreshCases();
  }, [refreshCases]);

  const refresh = useCallback(async () => {
    try {
      const [ts, ag] = await Promise.all([
        getTests({ status: statusFilter, page: 1, page_size: 50 }).catch(() => ({ tests: [], total: 0, page: 1, page_size: 50 })),
        getTestAgents().catch(() => ({ agents: [] })),
      ]);
      setTests(ts.tests);
      setAgents(ag.agents);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取测试列表失败");
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function handleStart() {
    if (!caseId.trim()) {
      toast.error("请填写 case_id");
      return;
    }
    setStarting(true);
    try {
      const payload: any = { case_id: caseId.trim(), version_type: versionType };
      if (versionType === "snapshot" && versionId) {
        payload.version_id = Number(versionId);
      }
      const resp = await startTest(payload);
      toast.success(`测试已启动：${resp.test_id.slice(0, 8)}`);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动测试失败");
    } finally {
      setStarting(false);
    }
  }

  async function handleRetry(testId: string) {
    try {
      const resp = await retryTest(testId);
      toast.success(`已重试：${resp.test_id.slice(0, 8)}`);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "重试失败");
    }
  }

  async function handleStop(testId: string) {
    try {
      await stopTest(testId);
      toast.success("已停止");
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "停止失败");
    }
  }

  async function handleDelete(testId: string) {
    if (!confirm("确认删除此测试记录？")) return;
    try {
      await deleteTest(testId);
      toast.success("已删除");
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    }
  }

  if (loading) return <div className="page-loading">加载测试列表…</div>;

  const snapshotAgents = agents.filter((a) => a.type === "snapshot");

  return (
    <div className="tests-page">
      <header className="page-header">
        <h1>单次测试</h1>
        <p className="page-desc">试验台：选数据集 + Agent 版本 → 跑一次 → 看结果（每 5 秒刷新）</p>
      </header>

      {/* 新建测试 */}
      <section className="test-start">
        <h3>新建测试</h3>
        <div className="test-form">
          <label className="test-field">
            <span>数据集 Case</span>
            <select
              className="evolve-select"
              value={caseId}
              onChange={(e) => setCaseId(e.target.value)}
              disabled={starting}
            >
              {casesLoadError ? (
                <option value="">（数据集加载失败，请稍后重试）</option>
              ) : cases.length === 0 ? (
                <option value="">（暂无数据集 case）</option>
              ) : null}
              {cases.map((c) => (
                <option key={c.case_id} value={c.case_id}>
                  [{c.layer}] {c.case_id} — {c.title}
                </option>
              ))}
            </select>
          </label>
          <label className="test-field">
            <span>版本类型</span>
            <select
              className="evolve-select"
              value={versionType}
              onChange={(e) => setVersionType(e.target.value)}
              disabled={starting}
            >
              <option value="working">working（未固化）</option>
              {snapshotAgents.length > 0 && <option value="snapshot">snapshot（快照）</option>}
            </select>
          </label>
          {versionType === "snapshot" && (
            <label className="test-field">
              <span>快照版本</span>
              <select
                className="evolve-select"
                value={versionId}
                onChange={(e) => setVersionId(e.target.value)}
                disabled={starting}
              >
                <option value="">选择版本…</option>
                {snapshotAgents.map((a) => (
                  <option key={a.version} value={a.version}>v{a.version}</option>
                ))}
              </select>
            </label>
          )}
          <button className="config-button primary" onClick={handleStart} disabled={starting}>
            {starting ? "启动中…" : "启动测试"}
          </button>
        </div>
      </section>

      {/* 过滤器 */}
      <div className="test-filter">
        <label>状态：</label>
        <select className="evolve-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="全部">全部</option>
          <option value="pending">待执行</option>
          <option value="running">运行中</option>
          <option value="done">完成</option>
          <option value="failed">失败</option>
        </select>
      </div>

      {/* 测试列表 */}
      <section className="test-list-section">
        {tests.length === 0 ? (
          <div className="monitor-empty">暂无测试记录</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Case</th>
                <th>版本</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {tests.map((t) => (
                <tr key={t.test_id}>
                  <td>{t.case_id}</td>
                  <td>{t.version_type === "snapshot" ? `v${t.version_id}` : "working"}</td>
                  <td><span className={`session-status ${t.status}`}>{testStatusLabel(t.status)}</span></td>
                  <td>{t.created_at.slice(0, 19).replace("T", " ")}</td>
                  <td className="test-actions">
                    {t.trace_id && (
                      <button className="action-link" onClick={() => navigate(`/traces/${t.trace_id}`)}>查看</button>
                    )}
                    {t.status === "failed" && (
                      <button className="action-link" onClick={() => handleRetry(t.test_id)}>重试</button>
                    )}
                    {(t.status === "pending" || t.status === "running") && (
                      <button className="action-link warn" onClick={() => handleStop(t.test_id)}>停止</button>
                    )}
                    {(t.status === "done" || t.status === "failed" || t.status === "cancelled") && (
                      <button className="action-link danger" onClick={() => handleDelete(t.test_id)}>删除</button>
                    )}
                    {t.error && <span className="test-error" title={t.error}>⚠</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function testStatusLabel(s: string): string {
  const map: Record<string, string> = {
    pending: "待执行", running: "运行中", done: "完成", failed: "失败", cancelled: "已取消",
  };
  return map[s] ?? s;
}
