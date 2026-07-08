
import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { login } from "@/lib/api";

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

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

  return (
    <main className="auth-page">
      <section className="auth-card">
        <h1 className="auth-title">思衍</h1>
        <p className="auth-subtitle">登录到你的创作工作区</p>
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
          <button className="auth-button" type="submit" disabled={submitting || !username.trim() || !password}>
            {submitting ? "登录中…" : "登录"}
          </button>
        </form>
        <p className="auth-foot">
          没有账号？
          <Link to="/register">凭邀请码注册</Link>
        </p>
      </section>
    </main>
  );
}
