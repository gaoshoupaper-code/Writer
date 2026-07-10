import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { login, fetchMeOrNull } from "@/lib/api";

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [checking, setChecking] = useState(true);

  // 已登录则跳首页。加 checking 锁防止 Shell↔Login 反弹闪烁：
  // 探测完成前不渲染任何内容，避免与 Shell 守卫的探测互相反弹。
  useEffect(() => {
    // 本地 dev 模式：executor 未启动时直接进首页（绕过登录）
    if (import.meta.env.DEV) {
      navigate("/", { replace: true });
      return;
    }
    let cancelled = false;
    fetchMeOrNull().then((me) => {
      if (cancelled) return;
      if (me) {
        navigate("/", { replace: true });
      } else {
        setChecking(false);
      }
    });
    return () => { cancelled = true; };
  }, [navigate]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      await login(username.trim(), password);
      navigate("/", { replace: true });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "登录失败。");
    } finally {
      setSubmitting(false);
    }
  }

  if (checking) {
    return <main className="auth-page"><div className="shell-loading">加载中…</div></main>;
  }

  return (
    <main className="auth-page">
      <section className="auth-card">
        <h1 className="auth-title">思衍进化</h1>
        <p className="auth-subtitle">登录到进化控制台</p>
        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-field">
            <span>用户名</span>
            <input
              className="auth-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="用户名"
              autoFocus
              autoComplete="username"
              disabled={submitting}
            />
          </label>
          <label className="auth-field">
            <span>密码</span>
            <input
              className="auth-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密码"
              autoComplete="current-password"
              disabled={submitting}
            />
          </label>
          <button
            className="auth-button"
            type="submit"
            disabled={submitting || !username.trim() || !password}
          >
            {submitting ? "登录中…" : "登录"}
          </button>
        </form>
        <p className="auth-foot">使用 executor 账号登录（SSO）</p>
      </section>
    </main>
  );
}
