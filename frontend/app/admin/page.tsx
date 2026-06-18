"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import {
  adminCreateInviteCodes,
  adminListInviteCodes,
  adminListUserWorkspaces,
  adminListUsers,
  adminReadUserWorkspaceOutline,
  adminResetUserPassword,
  adminRevokeInviteCode,
  adminUpdateUser,
  fetchMeOrNull,
  type AdminUserSummary,
  type AdminWorkspaceSummary,
  type InviteCodeSummary,
} from "@/lib/api";

type Tab = "codes" | "users";

export default function AdminPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("codes");

  useEffect(() => {
    let ignore = false;
    (async () => {
      const me = await fetchMeOrNull();
      if (ignore) return;
      if (!me) {
        router.replace("/login");
        return;
      }
      if (!me.is_admin) {
        router.replace("/");
        return;
      }
      setLoading(false);
    })();
    return () => { ignore = true; };
  }, [router]);

  if (loading) return <main className="auth-page"><p className="auth-subtitle">加载中…</p></main>;

  return (
    <main className="auth-page" style={{ alignItems: "flex-start", paddingTop: 40 }}>
      <section className="admin-card">
        <header className="admin-header">
          <h1 className="auth-title">管理后台</h1>
          <Link href="/" className="admin-back">← 返回工作区</Link>
        </header>

        <nav className="admin-tabs">
          <button
            className={`admin-tab ${tab === "codes" ? "active" : ""}`}
            type="button"
            onClick={() => setTab("codes")}
          >
            邀请码
          </button>
          <button
            className={`admin-tab ${tab === "users" ? "active" : ""}`}
            type="button"
            onClick={() => setTab("users")}
          >
            用户
          </button>
        </nav>

        {tab === "codes" ? <InviteCodesPanel /> : null}
        {tab === "users" ? <UsersPanel /> : null}
      </section>
    </main>
  );
}

// ── 邀请码面板 ────────────────────────────────────────────

function InviteCodesPanel() {
  const [codes, setCodes] = useState<InviteCodeSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newCodes, setNewCodes] = useState<string[]>([]);

  async function reload() {
    setLoading(true);
    try {
      setCodes(await adminListInviteCodes());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载邀请码失败。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { reload(); }, []);

  async function handleCreate() {
    if (creating) return;
    setCreating(true);
    try {
      const generated = await adminCreateInviteCodes(1);
      setNewCodes(generated);
      toast.success("已生成 1 个邀请码");
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "生成失败。");
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(code: string) {
    if (!confirm("确定吊销这个邀请码？")) return;
    try {
      await adminRevokeInviteCode(code);
      toast.success("已吊销");
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "吊销失败。");
    }
  }

  function copyCode(code: string) {
    navigator.clipboard?.writeText(code).then(
      () => toast.success("已复制"),
      () => toast.error("复制失败"),
    );
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <h2>邀请码</h2>
        <button className="auth-button" type="button" onClick={handleCreate} disabled={creating} style={{ marginTop: 0, width: "auto" }}>
          {creating ? "生成中…" : "生成邀请码"}
        </button>
      </div>

      {newCodes.length > 0 ? (
        <div className="admin-newcodes">
          <strong>新生成的邀请码（复制后发给用户）：</strong>
          {newCodes.map((c) => (
            <div key={c} className="admin-code-row">
              <code className="admin-code">{c}</code>
              <button type="button" className="admin-copy" onClick={() => copyCode(c)}>复制</button>
            </div>
          ))}
        </div>
      ) : null}

      {loading ? (
        <p className="auth-subtitle">加载中…</p>
      ) : codes.length === 0 ? (
        <p className="auth-subtitle">暂无邀请码。</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>邀请码</th>
              <th>类型</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {codes.map((c) => {
              const usable = !c.used && !c.revoked_at;
              return (
                <tr key={c.code}>
                  <td><code className="admin-code">{c.code}</code></td>
                  <td>{c.is_admin_code ? "管理员" : "普通"}</td>
                  <td>
                    {c.revoked_at ? <span className="badge-revoked">已吊销</span>
                      : c.used ? <span className="badge-used">已使用</span>
                      : <span className="badge-usable">可用</span>}
                  </td>
                  <td className="admin-time">{c.created_at.slice(0, 19).replace("T", " ")}</td>
                  <td>
                    {usable ? (
                      <>
                        <button type="button" className="admin-link" onClick={() => copyCode(c.code)}>复制</button>
                        <button type="button" className="admin-link danger" onClick={() => handleRevoke(c.code)}>吊销</button>
                      </>
                    ) : <span className="admin-muted">—</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── 用户面板 ──────────────────────────────────────────────

function UsersPanel() {
  const [users, setUsers] = useState<AdminUserSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<AdminUserSummary | null>(null);

  async function reload() {
    setLoading(true);
    try {
      setUsers(await adminListUsers());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载用户失败。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { reload(); }, []);

  async function handleToggleDisable(u: AdminUserSummary) {
    const next = !u.disabled;
    const verb = next ? "禁用" : "启用";
    if (!confirm(`确定${verb}用户「${u.username}」？`)) return;
    try {
      await adminUpdateUser(u.user_id, { disabled: next });
      toast.success(`已${verb}`);
      await reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : `${verb}失败。`);
    }
  }

  async function handleResetPassword(u: AdminUserSummary) {
    if (!confirm(`确定为「${u.username}」重置密码？会生成临时密码。`)) return;
    try {
      const res = await adminResetUserPassword(u.user_id);
      alert(`用户「${u.username}」的临时密码：\n\n${res.temp_password}\n\n请私下发给用户。`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "重置失败。");
    }
  }

  if (viewing) {
    return <UserWorkspacesView user={viewing} onBack={() => setViewing(null)} />;
  }

  return (
    <div className="admin-section">
      <h2>用户（{users.length}）</h2>
      {loading ? (
        <p className="auth-subtitle">加载中…</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>角色</th>
              <th>状态</th>
              <th>Key</th>
              <th>作品数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.user_id} className={u.disabled ? "row-disabled" : ""}>
                <td>{u.username}</td>
                <td>{u.is_admin ? "管理员" : "普通"}</td>
                <td>{u.disabled ? <span className="badge-revoked">已禁用</span> : <span className="badge-usable">正常</span>}</td>
                <td>{u.has_api_key ? "✓" : "—"}</td>
                <td>{u.workspace_count}</td>
                <td>
                  <button type="button" className="admin-link" onClick={() => setViewing(u)}>查看作品</button>
                  {!u.is_admin ? (
                    <button type="button" className="admin-link" onClick={() => handleResetPassword(u)}>重置密码</button>
                  ) : null}
                  {!u.is_admin ? (
                    <button type="button" className="admin-link danger" onClick={() => handleToggleDisable(u)}>
                      {u.disabled ? "启用" : "禁用"}
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── 代访问作品（D14 只读）──────────────────────────────────

function UserWorkspacesView({ user, onBack }: { user: AdminUserSummary; onBack: () => void }) {
  const [workspaces, setWorkspaces] = useState<AdminWorkspaceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeWs, setActiveWs] = useState<AdminWorkspaceSummary | null>(null);
  const [outline, setOutline] = useState<string>("");
  const [outlineLoading, setOutlineLoading] = useState(false);

  useEffect(() => {
    let ignore = false;
    (async () => {
      try {
        const ws = await adminListUserWorkspaces(user.user_id);
        if (ignore) return;
        setWorkspaces(ws);
        setActiveWs(ws[0] ?? null);
      } catch (err) {
        if (!ignore) toast.error(err instanceof Error ? err.message : "加载作品失败。");
      } finally {
        if (!ignore) setLoading(false);
      }
    })();
    return () => { ignore = true; };
  }, [user.user_id]);

  useEffect(() => {
    if (!activeWs) { setOutline(""); return; }
    let ignore = false;
    setOutlineLoading(true);
    (async () => {
      try {
        const data = await adminReadUserWorkspaceOutline(user.user_id, activeWs.workspace_id);
        if (!ignore) setOutline(data.markdown);
      } catch {
        if (!ignore) setOutline("");
      } finally {
        if (!ignore) setOutlineLoading(false);
      }
    })();
    return () => { ignore = true; };
  }, [user.user_id, activeWs]);

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <button type="button" className="admin-link" onClick={onBack}>← 返回用户列表</button>
        <h2>{user.username} 的作品（只读）</h2>
      </div>

      {loading ? <p className="auth-subtitle">加载中…</p> : workspaces.length === 0 ? (
        <p className="auth-subtitle">该用户暂无作品。</p>
      ) : (
        <div className="admin-ws-layout">
          <aside className="admin-ws-list">
            {workspaces.map((w) => (
              <button
                key={w.workspace_id}
                type="button"
                className={`admin-ws-item ${activeWs?.workspace_id === w.workspace_id ? "active" : ""}`}
                onClick={() => setActiveWs(w)}
              >
                {w.title}
              </button>
            ))}
          </aside>
          <div className="admin-ws-content">
            {activeWs ? (
              <>
                <h3>{activeWs.title} · 大纲</h3>
                {outlineLoading ? <p className="auth-subtitle">加载中…</p> : (
                  <pre className="admin-outline">{outline || "（暂无大纲内容）"}</pre>
                )}
                <p className="auth-hint">管理员只读访问，不可编辑。</p>
              </>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
