import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  getLlmConfig,
  putLlmConfig,
  testLlmConfig,
  type LlmConfigOut,
  type LlmConfigTestResult,
} from "@/lib/api";

/**
 * 大模型 API 配置页（桌面化改造核心落地点，2026-07-07）。
 *
 * 桌面端唯一配置入口：填 base_url / api_key / model → PUT evolution 加密存。
 * GET 不回显 key（安全），编辑时 key 留空=不改。
 * 支持连通性测试（POST /test，不落库）。
 */
export default function ConfigPage() {
  const [config, setConfig] = useState<LlmConfigOut | null>(null);
  const [loading, setLoading] = useState(true);

  // 表单字段
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  // 编辑态：key 留空表示不改（GET 不回显，编辑时若不改 key 则提交时用占位）
  const [keyDirty, setKeyDirty] = useState(false);

  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LlmConfigTestResult | null>(null);

  useEffect(() => {
    getLlmConfig()
      .then((c) => {
        setConfig(c);
        setBaseUrl(c.base_url);
        setModel(c.model);
      })
      .catch((err) => toast.error(err instanceof Error ? err.message : "读取配置失败"))
      .finally(() => setLoading(false));
  }, []);

  async function handleSave() {
    if (!baseUrl.trim() || !model.trim()) {
      toast.error("base_url 和 model 不能为空");
      return;
    }
    if (config?.has_key && !keyDirty && !apiKey) {
      toast.error("请填写 api_key（编辑时不回显，需重新输入）");
      return;
    }
    if (!config?.has_key && !apiKey) {
      toast.error("首次配置必须填写 api_key");
      return;
    }
    setSaving(true);
    try {
      await putLlmConfig({
        api_key: apiKey,
        base_url: baseUrl.trim(),
        model: model.trim(),
      });
      toast.success("配置已保存");
      // 刷新状态
      const fresh = await getLlmConfig();
      setConfig(fresh);
      setApiKey("");
      setKeyDirty(false);
      setTestResult(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (!baseUrl.trim() || !model.trim()) {
      toast.error("请先填写 base_url 和 model");
      return;
    }
    // 测试：有已存 key 且未改 key → 用已存的（后端读库）；否则必须填了新 key
    const keyForTest = apiKey;
    if (!keyForTest && !config?.has_key) {
      toast.error("请先填写 api_key");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      // 若 key 留空（用已存的），传占位让后端 test 接口知道用库里的
      // 但 test 接口设计是"用提交的临时 key"，不读库。所以留空时提示填。
      const result = await testLlmConfig({
        api_key: keyForTest || "__use_stored__",
        base_url: baseUrl.trim(),
        model: model.trim(),
      });
      setTestResult(result);
      if (result.ok) {
        toast.success(`连通正常（${result.latency_ms}ms）`);
      } else {
        toast.error(`连通失败：${result.error}`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "测试失败");
    } finally {
      setTesting(false);
    }
  }

  if (loading) {
    return <div className="page-loading">加载配置…</div>;
  }

  return (
    <div className="config-page">
      <header className="page-header">
        <h1>大模型 API 配置</h1>
        <p className="page-desc">
          进化端的 LLM 配置（judge 评分 + 进化 Agent 共用一套）。填完后加密存储在服务器，
          运行时解密调用。
        </p>
      </header>

      <section className="config-form">
        <div className="config-status">
          {config?.has_key ? (
            <span className="status-badge ok">● 已配置</span>
          ) : (
            <span className="status-badge warn">○ 未配置</span>
          )}
          {config?.updated_at && (
            <span className="status-time">
              更新于 {new Date(config.updated_at).toLocaleString()}
            </span>
          )}
        </div>

        <label className="config-field">
          <span className="config-label">Base URL</span>
          <input
            className="config-input"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.deepseek.com"
            disabled={saving || testing}
          />
          <span className="config-hint">OpenAI 兼容端点</span>
        </label>

        <label className="config-field">
          <span className="config-label">API Key</span>
          <input
            className="config-input"
            type="password"
            value={apiKey}
            onChange={(e) => {
              setApiKey(e.target.value);
              setKeyDirty(true);
            }}
            placeholder={config?.has_key ? "已配置（编辑时重新输入）" : "sk-..."}
            disabled={saving || testing}
            autoComplete="off"
          />
          <span className="config-hint">
            {config?.has_key
              ? "已加密存储，编辑时需重新填写（安全起见不回显）"
              : "加密存储，不会明文返回"}
          </span>
        </label>

        <label className="config-field">
          <span className="config-label">Model</span>
          <input
            className="config-input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="deepseek-chat"
            disabled={saving || testing}
          />
          <span className="config-hint">模型名（如 deepseek-chat / glm-4.6 / gpt-4o）</span>
        </label>

        {testResult && (
          <div className={`config-test-result ${testResult.ok ? "ok" : "fail"}`}>
            {testResult.ok
              ? `✓ 连通正常，延迟 ${testResult.latency_ms}ms`
              : `✗ 连通失败：${testResult.error}`}
          </div>
        )}

        <div className="config-actions">
          <button
            className="config-button primary"
            onClick={handleSave}
            disabled={saving || testing}
          >
            {saving ? "保存中…" : "保存配置"}
          </button>
          <button
            className="config-button secondary"
            onClick={handleTest}
            disabled={saving || testing}
          >
            {testing ? "测试中…" : "测试连通性"}
          </button>
        </div>
      </section>
    </div>
  );
}
