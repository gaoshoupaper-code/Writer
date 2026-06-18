"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import { clearApiKey, fetchMeOrNull, fetchMyProfile, setApiKey } from "@/lib/api";

export default function SettingsPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [hasKey, setHasKey] = useState(false);
  const [apiKey, setApiKeyInput] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [quota, setQuota] = useState<{ count: number; quota: number } | null>(null);

  useEffect(() => {
    let ignore = false;
    (async () => {
      const me = await fetchMeOrNull();
      if (!me) {
        if (!ignore) router.replace("/login");
        return;
      }
      try {
        const profile = await fetchMyProfile();
        if (ignore) return;
        setHasKey(profile.has_api_key);
        setBaseUrl(profile.base_url ?? "");
        setQuota({ count: profile.workspace_count, quota: profile.workspace_quota });
      } catch (err) {
        if (!ignore) toast.error(err instanceof Error ? err.message : "加载设置失败。");
      } finally {
        if (!ignore) setLoading(false);
      }
    })();
    return () => { ignore = true; };
  }, [router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const key = apiKey.trim();
    if (!key) {
      toast.error("API Key 不能为空。");
      return;
    }
    setSubmitting(true);
    try {
      await setApiKey(key, baseUrl.trim());
      setHasKey(true);
      setApiKeyInput("");
      toast.success("API Key 已保存。");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleClear() {
    if (submitting) return;
    setSubmitting(true);
    try {
      await clearApiKey();
      setHasKey(false);
      setApiKeyInput("");
      toast.success("已清除 API Key。");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "清除失败。");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <main className="auth-page"><p className="auth-subtitle">加载中…</p></main>;
  }

  return (
    <main className="auth-page">
      <section className="auth-card" style={{ maxWidth: 560 }}>
        <h1 className="auth-title">设置</h1>
        <p className="auth-subtitle">配置你的 LLM API Key（平台加密存储）</p>

        {quota ? (
          <div className="settings-quota">
            作品配额：{quota.count} / {quota.quota}
          </div>
        ) : null}

        <div className="settings-key-status">
          当前状态：
          <strong className={hasKey ? "settings-key-ok" : "settings-key-missing"}>
            {hasKey ? "已配置 API Key" : "未配置（AI 功能不可用）"}
          </strong>
        </div>

        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-field">
            <span>API Key</span>
            <input
              className="auth-input"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKeyInput(e.target.value)}
              placeholder={hasKey ? "输入新 Key 以替换（留空不修改）" : "sk-..."}
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
              placeholder="https://api.openai.com/v1"
              autoComplete="off"
              disabled={submitting}
            />
          </label>
          <div className="modal-actions">
            {hasKey ? (
              <button className="modal-button modal-cancel" type="button" onClick={handleClear} disabled={submitting}>
                清除 Key
              </button>
            ) : null}
            <button className="auth-button" type="submit" disabled={submitting || !apiKey.trim()}>
              {submitting ? "保存中…" : "保存"}
            </button>
          </div>
        </form>

        <p className="auth-hint">
          平台使用 AES-256-GCM 加密存储你的 Key。忘记密码需重置，重置后 Key 会清空，需重新填写。
        </p>

        <p className="auth-foot">
          <Link href="/">返回工作区</Link>
        </p>
      </section>
    </main>
  );
}
