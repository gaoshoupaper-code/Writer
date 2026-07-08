import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  listLlmConfigs,
  createLlmConfig,
  updateLlmConfig,
  deleteLlmConfig,
  activateLlmConfig,
  testLlmConfig,
  type LlmConfigItem,
  type LlmConfigTestResult,
} from "@/lib/api";

/**
 * 大模型 API 配置页（多配置管理，2026-07-08）。
 *
 * 支持保存多个配置（deepseek/glm/openai 等），列表展示（name/base_url/model/尾4位 key 脱敏），
 * 选一个激活（runtime 读激活项），可改/删，可对任意一条做连通性测试（后端读库解密，无需重输 key）。
 */

/** 编辑表单的状态：editingId=null=新建，否则=编辑该 id。 */
interface FormState {
  editingId: number | null;
  name: string;
  apiKey: string;
  baseUrl: string;
  model: string;
}

const EMPTY_FORM: FormState = {
  editingId: null,
  name: "",
  apiKey: "",
  baseUrl: "",
  model: "",
};

export default function ConfigPage() {
  const [configs, setConfigs] = useState<LlmConfigItem[]>([]);
  const [loading, setLoading] = useState(true);

  // 编辑/新建表单：null=隐藏，对象=显示
  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);

  // 每条配置独立的测试状态（id → 结果 + loading）
  const [testingIds, setTestingIds] = useState<Set<number>>(new Set());
  const [testResults, setTestResults] = useState<Record<number, LlmConfigTestResult>>({});

  // 草稿测试（新建表单里点测试）
  const [draftTesting, setDraftTesting] = useState(false);
  const [draftTestResult, setDraftTestResult] = useState<LlmConfigTestResult | null>(null);

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    try {
      const list = await listLlmConfigs();
      setConfigs(list);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取配置失败");
    } finally {
      setLoading(false);
    }
  }

  function openCreate() {
    setForm({ ...EMPTY_FORM });
    setDraftTestResult(null);
  }

  function openEdit(item: LlmConfigItem) {
    setForm({
      editingId: item.id,
      name: item.name,
      apiKey: "", // 编辑时不回显 key
      baseUrl: item.base_url,
      model: item.model,
    });
    setDraftTestResult(null);
  }

  function closeForm() {
    setForm(null);
    setDraftTestResult(null);
  }

  async function handleSave() {
    if (!form) return;
    if (!form.name.trim() || !form.baseUrl.trim() || !form.model.trim()) {
      toast.error("名称 / Base URL / Model 不能为空");
      return;
    }
    // 新建必须填 key；编辑时 key 留空=不改
    if (form.editingId === null && !form.apiKey) {
      toast.error("首次配置必须填写 API Key");
      return;
    }
    setSaving(true);
    try {
      if (form.editingId === null) {
        await createLlmConfig({
          name: form.name.trim(),
          api_key: form.apiKey,
          base_url: form.baseUrl.trim(),
          model: form.model.trim(),
        });
        toast.success("配置已新建");
      } else {
        await updateLlmConfig(form.editingId, {
          name: form.name.trim(),
          api_key: form.apiKey || undefined, // 空=不改
          base_url: form.baseUrl.trim(),
          model: form.model.trim(),
        });
        toast.success("配置已更新");
      }
      closeForm();
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(item: LlmConfigItem) {
    if (!confirm(`确认删除配置「${item.name}」？此操作不可撤销。`)) return;
    try {
      await deleteLlmConfig(item.id);
      toast.success("已删除");
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    }
  }

  async function handleActivate(item: LlmConfigItem) {
    try {
      await activateLlmConfig(item.id);
      toast.success(`已激活「${item.name}」`);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "激活失败");
    }
  }

  /** 测试已保存配置：后端读库解密，无需 key。 */
  async function handleTestSaved(item: LlmConfigItem) {
    setTestingIds((s) => new Set(s).add(item.id));
    setTestResults((r) => {
      const next = { ...r };
      delete next[item.id];
      return next;
    });
    try {
      const result = await testLlmConfig({ id: item.id });
      setTestResults((r) => ({ ...r, [item.id]: result }));
      if (result.ok) {
        toast.success(`「${item.name}」连通正常（${result.latency_ms}ms）`);
      } else {
        toast.error(`「${item.name}」连通失败：${result.error}`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "测试失败");
    } finally {
      setTestingIds((s) => {
        const next = new Set(s);
        next.delete(item.id);
        return next;
      });
    }
  }

  /** 测试草稿（表单里填的临时值，不落库）。 */
  async function handleTestDraft() {
    if (!form) return;
    if (!form.baseUrl.trim() || !form.model.trim()) {
      toast.error("请先填写 Base URL 和 Model");
      return;
    }
    // 编辑态若没重填 key，又要点测试 → 提示先保存或重填
    if (form.editingId !== null && !form.apiKey) {
      toast.error("编辑态测试需重填 API Key（或保存后用列表的测试按钮，读库免输入）");
      return;
    }
    if (!form.apiKey) {
      toast.error("请填写 API Key 后测试");
      return;
    }
    setDraftTesting(true);
    setDraftTestResult(null);
    try {
      const result = await testLlmConfig({
        api_key: form.apiKey,
        base_url: form.baseUrl.trim(),
        model: form.model.trim(),
      });
      setDraftTestResult(result);
      if (result.ok) {
        toast.success(`连通正常（${result.latency_ms}ms）`);
      } else {
        toast.error(`连通失败：${result.error}`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "测试失败");
    } finally {
      setDraftTesting(false);
    }
  }

  if (loading) {
    return <div className="page-loading">加载配置…</div>;
  }

  const activeItem = configs.find((c) => c.is_active);

  return (
    <div className="config-page">
      <header className="page-header">
        <h1>大模型 API 配置</h1>
        <p className="page-desc">
          进化端的 LLM 配置（judge 评分 + 进化 Agent 共用一套）。可保存多个配置，
          选一个激活——运行时读激活项。API Key 加密存储在服务器，运行时解密调用。
        </p>
      </header>

      {/* 激活状态条 */}
      <section className="config-status">
        {activeItem ? (
          <>
            <span className="status-badge ok">● 已激活：{activeItem.name}</span>
            <span className="status-meta">
              {activeItem.model} · {activeItem.base_url}
            </span>
          </>
        ) : (
          <span className="status-badge warn">○ 暂无激活配置</span>
        )}
      </section>

      {/* 新建按钮 */}
      <div className="config-toolbar">
        <button className="config-button primary" onClick={openCreate} disabled={!!form}>
          + 新建配置
        </button>
      </div>

      {/* 编辑/新建表单 */}
      {form && (
        <section className="config-form">
          <div className="config-form-title">
            {form.editingId === null ? "新建配置" : "编辑配置"}
          </div>
          <label className="config-field">
            <span className="config-label">名称</span>
            <input
              className="config-input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="如 deepseek-主力"
              disabled={saving || draftTesting}
            />
          </label>
          <label className="config-field">
            <span className="config-label">API Key</span>
            <input
              className="config-input"
              type="password"
              value={form.apiKey}
              onChange={(e) => setForm({ ...form, apiKey: e.target.value })}
              placeholder={form.editingId !== null ? "留空=不修改" : "sk-..."}
              disabled={saving || draftTesting}
              autoComplete="off"
            />
            <span className="config-hint">
              {form.editingId !== null
                ? "加密存储，编辑时留空表示不改 key"
                : "加密存储，不会明文返回"}
            </span>
          </label>
          <label className="config-field">
            <span className="config-label">Base URL</span>
            <input
              className="config-input"
              value={form.baseUrl}
              onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
              placeholder="https://api.deepseek.com"
              disabled={saving || draftTesting}
            />
            <span className="config-hint">OpenAI 兼容端点</span>
          </label>
          <label className="config-field">
            <span className="config-label">Model</span>
            <input
              className="config-input"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
              placeholder="deepseek-chat / glm-4.6 / gpt-4o"
              disabled={saving || draftTesting}
            />
          </label>

          {draftTestResult && (
            <div className={`config-test-result ${draftTestResult.ok ? "ok" : "fail"}`}>
              {draftTestResult.ok
                ? `✓ 连通正常，延迟 ${draftTestResult.latency_ms}ms`
                : `✗ 连通失败：${draftTestResult.error}`}
            </div>
          )}

          <div className="config-actions">
            <button
              className="config-button primary"
              onClick={handleSave}
              disabled={saving || draftTesting}
            >
              {saving ? "保存中…" : "保存"}
            </button>
            <button
              className="config-button secondary"
              onClick={handleTestDraft}
              disabled={saving || draftTesting}
            >
              {draftTesting ? "测试中…" : "测试连通性"}
            </button>
            <button className="config-button ghost" onClick={closeForm} disabled={saving}>
              取消
            </button>
          </div>
        </section>
      )}

      {/* 配置列表 */}
      <section className="config-list">
        {configs.length === 0 ? (
          <div className="config-empty">
            还没有配置。点击「+ 新建配置」添加你的第一个大模型 API。
          </div>
        ) : (
          configs.map((item) => (
            <div key={item.id} className={`config-card ${item.is_active ? "active" : ""}`}>
              <div className="config-card-head">
                <span className="config-card-name">
                  {item.is_active && <span className="config-active-dot" />}
                  {item.name}
                </span>
                {item.is_active && <span className="config-active-tag">当前激活</span>}
              </div>
              <div className="config-card-meta">
                <span title={item.model}>{item.model}</span>
                <span className="config-card-sep">·</span>
                <span title={item.base_url}>{item.base_url}</span>
              </div>
              <div className="config-card-key">
                API Key：
                {item.has_key
                  ? `••••${item.key_hint ?? ""}`
                  : <span className="config-key-missing">未填写</span>}
              </div>
              {testResults[item.id] && (
                <div
                  className={`config-test-result ${
                    testResults[item.id].ok ? "ok" : "fail"
                  }`}
                >
                  {testResults[item.id].ok
                    ? `✓ 连通正常，延迟 ${testResults[item.id].latency_ms}ms`
                    : `✗ 连通失败：${testResults[item.id].error}`}
                </div>
              )}
              <div className="config-card-actions">
                <button
                  className="config-button small"
                  onClick={() => handleTestSaved(item)}
                  disabled={testingIds.has(item.id)}
                >
                  {testingIds.has(item.id) ? "测试中…" : "测试"}
                </button>
                <button className="config-button small" onClick={() => openEdit(item)}>
                  编辑
                </button>
                {!item.is_active && (
                  <button
                    className="config-button small"
                    onClick={() => handleActivate(item)}
                  >
                    设为激活
                  </button>
                )}
                <button
                  className="config-button small danger"
                  onClick={() => handleDelete(item)}
                >
                  删除
                </button>
              </div>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
