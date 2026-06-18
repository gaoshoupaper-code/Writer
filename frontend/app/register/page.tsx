"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import { register } from "@/lib/api";

export default function RegisterPage() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    if (password !== confirm) {
      toast.error("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    try {
      await register(code.trim(), username.trim(), password);
      router.replace("/");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "注册失败。");
    } finally {
      setSubmitting(false);
    }
  }

  const ready = code.trim() && username.trim().length >= 2 && password.length >= 6 && password === confirm;

  return (
    <main className="auth-page">
      <section className="auth-card">
        <h1 className="auth-title">注册账号</h1>
        <p className="auth-subtitle">使用邀请码创建你的账号</p>
        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-field">
            <span>邀请码</span>
            <input
              className="auth-input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="输入管理员发给你的邀请码"
              autoFocus
              disabled={submitting}
            />
          </label>
          <label className="auth-field">
            <span>用户名</span>
            <input
              className="auth-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="至少 2 个字符"
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
              placeholder="至少 6 个字符"
              autoComplete="new-password"
              disabled={submitting}
            />
          </label>
          <label className="auth-field">
            <span>确认密码</span>
            <input
              className="auth-input"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="再次输入密码"
              autoComplete="new-password"
              disabled={submitting}
            />
          </label>
          <button className="auth-button" type="submit" disabled={submitting || !ready}>
            {submitting ? "注册中…" : "注册并登录"}
          </button>
        </form>
        <p className="auth-foot">
          已有账号？
          <Link href="/login">直接登录</Link>
        </p>
        <p className="auth-hint">
          忘记密码只能联系管理员重置；请妥善保管。注册后请在设置页填写你自己的 API Key。
        </p>
      </section>
    </main>
  );
}
