"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import {
  activateProviderConfig,
  clearApiKey,
  createProviderConfig,
  deleteProviderConfig,
  fetchMeOrNull,
  fetchMyProfile,
  listProviderConfigs,
  updateProviderConfig,
  type ProviderConfig,
} from "@/lib/api";

type FormMode =
  | { kind: "new" }
  | { kind: "edit"; config: ProviderConfig };

export default function SettingsPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [configs, setConfigs] = useState<ProviderConfig[]>([]);
  const [quota, setQuota] = useState<{ count: number; quota: number } | null>(null);
  const [activeConfigId, setActiveConfigId] = useState<string | null>(null);
  const [mode, setMode] = useState<FormMode>({ kind: "new" });
  const [submitting, setSubmitting] = useState(false);

  // 表单字段
  const [name, setName] = useState("");
  const [apiKey, setApiKeyInput] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [activate, setActivate] = useState(true);

  async function reload() {
    try {
      const [profile, list] = await Promise.all([fetchMyProfile(), listProviderConfigs()]);
      setQuota({ count: profile.workspace_count, quota: profile.workspace_quota });
      setConfigs(list);
      const active = list.find((c) => c.is_active);
      setActiveConfigId(active?.config_id ?? null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载设置失败。");
    }
  }

  useEffect(() => {
    let ignore = false;
    (async () => {
      const me = await fetchMeOrNull();
      if (ignore) return;
      if (!me) {
        router.replace("/login");
        return;
      }
      await reload();
      if (!ignore) setLoading(false);
    })();
    return () => { ignore = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  function resetForm() {
    setMode({ kind: "new" });
    setName("");
    setApiKeyInput("");
    setBaseUrl("");
    setModel("");
    setActivate(true);
  }

  function startEdit(c: ProviderConfig) {
    setMode({ kind: "edit", config: c });
    setName(c.name);
    setApiKeyInput(""); // 编辑时不回显 key（安全），留空表示不改
    setBaseUrl(c.base_url ?? "");
    setModel(c.model);
    setActivate(c.is_active);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    if (!name.trim() || !model.trim()) {
      toast.error("名称和模型不能为空。");
      return;
    }
    if (mode.kind === "new" && !apiKey.trim()) {
      toast.error("新建配置时 API Key 不能为空。");
      return;
    }
    setSubmitting(true);
    try {
      if (mode.kind === "new") {
        await createProviderConfig({
          name: name.trim(),
          api_key: apiKey.trim(),
          base_url: baseUrl.trim() || null,
          model: model.trim(),
          activate,
        });
        toast.success("配置已保存。");
      } else {
        const patch: Record<string, unknown> = {
          name: name.trim(),
          base_url: baseUrl.trim() || null,
          model: model.trim(),
        };
        if (apiKey.trim()) patch.api_key = apiKey.trim();
        await updateProviderConfig(mode.config.config_id, patch);
        if (activate && !mode.config.is_active) {
          await activateProviderConfig(mode.config.config_id);
        }
        toast.success("配置已更新。");
      }
      resetForm();
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleActivate(configId: string) {
    if (submitting) return;
    setSubmitting(true);
    try {
      await activateProviderConfig(configId);
      toast.success("已切换为当前使用配置。");
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "切换失败。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(configId: string, configName: string) {
    if (!confirm(`确定删除配置「${configName}」？此操作不可撤销。`)) return;
    if (submitting) return;
    setSubmitting(true);
    try {
      await deleteProviderConfig(configId);
      toast.success("已删除。");
      if (mode.kind === "edit" && mode.config.config_id === configId) resetForm();
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleClearActive() {
    if (!activeConfigId) return;
    if (!confirm("清除当前激活的配置？清除后 AI 功能不可用，直到重新激活。")) return;
    setSubmitting(true);
    try {
      await clearApiKey();
      toast.success("已清除当前激活配置。");
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "清除失败。");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <main className="auth-page"><p className="auth-subtitle">加载中…</p></main>;
  }

  const hasActive = activeConfigId !== null;
  const activeConfig = configs.find((c) => c.config_id === activeConfigId) ?? null;

  return (
    <main className="auth-page" style={{ alignItems: "flex-start", paddingTop: 40 }}>
      <section className="admin-card" style={{ maxWidth: 880 }}>
        <header className="admin-header">
          <h1 className="auth-title">API 配置</h1>
          <Link href="/" className="admin-back">← 返回工作区</Link>
        </header>

        {quota ? (
          <div className="settings-quota">作品配额：{quota.count} / {quota.quota}</div>
        ) : null}

        <div className="settings-key-status">
          当前使用：
          {hasActive && activeConfig ? (
            <strong className="settings-key-ok">
              {activeConfig.name}（{activeConfig.model}）
            </strong>
          ) : (
            <strong className="settings-key-missing">未配置（AI 功能不可用）</strong>
          )}
        </div>

        <div className="provider-layout">
          {/* 左侧：配置列表 */}
          <aside className="provider-list">
            <div className="provider-list-head">
              <span>我的配置（{configs.length}）</span>
              <button type="button" className="admin-link" onClick={resetForm}>+ 新建</button>
            </div>
            {configs.length === 0 ? (
              <p className="auth-hint" style={{ textAlign: "left" }}>还没有配置，点「新建」添加。</p>
            ) : (
              configs.map((c) => (
                <div
                  key={c.config_id}
                  className={`provider-item ${c.config_id === activeConfigId ? "active" : ""} ${mode.kind === "edit" && mode.config.config_id === c.config_id ? "editing" : ""}`}
                >
                  <div className="provider-item-main" onClick={() => startEdit(c)}>
                    <div className="provider-item-name">
                      {c.name}
                      {c.is_active ? <span className="provider-badge">使用中</span> : null}
                    </div>
                    <div className="provider-item-meta">{c.model} · {c.base_url || "默认地址"}</div>
                  </div>
                  <div className="provider-item-actions">
                    {!c.is_active ? (
                      <button type="button" className="admin-link" onClick={() => handleActivate(c.config_id)}>切换</button>
                    ) : null}
                    <button type="button" className="admin-link danger" onClick={() => handleDelete(c.config_id, c.name)}>删除</button>
                  </div>
                </div>
              ))
            )}
            {hasActive ? (
              <button type="button" className="admin-link danger" style={{ marginTop: 12 }} onClick={handleClearActive}>
                清除当前激活
              </button>
            ) : null}
          </aside>

          {/* 右侧：表单 */}
          <div className="provider-form">
            <h3>{mode.kind === "new" ? "新建配置" : `编辑「${mode.config.name}」`}</h3>
            <form className="auth-form" onSubmit={handleSubmit}>
              <label className="auth-field">
                <span>名称（方便区分，如「我的GLM」）</span>
                <input
                  className="auth-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="给这套配置起个名字"
                  disabled={submitting}
                />
              </label>
              <label className="auth-field">
                <span>API Key {mode.kind === "edit" ? "（留空不修改）" : ""}</span>
                <input
                  className="auth-input"
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder={mode.kind === "edit" ? "••••••（已加密保存）" : "sk-..."}
                  autoComplete="off"
                  disabled={submitting}
                />
              </label>
              <label className="auth-field">
                <span>Base URL（可选）</span>
                <input
                  className="auth-input"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://open.bigmodel.cn/api/paas/v4"
                  autoComplete="off"
                  disabled={submitting}
                />
              </label>
              <label className="auth-field">
                <span>模型名称</span>
                <input
                  className="auth-input"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="glm-4.6 / deepseek-chat / gpt-4o 等"
                  autoComplete="off"
                  disabled={submitting}
                />
              </label>
              <label className="auth-checkbox">
                <input
                  type="checkbox"
                  checked={activate}
                  onChange={(e) => setActivate(e.target.checked)}
                  disabled={submitting}
                />
                <span>保存后立即设为当前使用</span>
              </label>
              <div className="modal-actions">
                {mode.kind === "edit" ? (
                  <button className="modal-button modal-cancel" type="button" onClick={resetForm} disabled={submitting}>
                    取消编辑
                  </button>
                ) : null}
                <button className="auth-button" type="submit" disabled={submitting} style={{ marginTop: 0, width: "auto" }}>
                  {submitting ? "保存中…" : mode.kind === "new" ? "保存配置" : "更新配置"}
                </button>
              </div>
            </form>
            <p className="auth-hint">
              平台用 AES-256-GCM 加密存储你的 Key。列表只显示名称/模型，不显示 Key 明文。可保存多套配置方便切换。
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
