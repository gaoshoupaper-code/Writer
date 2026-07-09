"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  fetchUsers,
  adjustCredits,
  resetPassword,
  updateUser,
  type AdminUser,
} from "@/lib/admin-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";

export default function UsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [creditsTarget, setCreditsTarget] = useState<AdminUser | null>(null);
  const [creditsAmount, setCreditsAmount] = useState("");
  const [creditsNote, setCreditsNote] = useState("");

  const load = useCallback(async () => {
    try {
      setUsers(await fetchUsers());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleAdjustCredits = async () => {
    if (!creditsTarget) return;
    const amt = parseInt(creditsAmount, 10);
    if (isNaN(amt)) return toast.error("请输入有效数字");
    try {
      await adjustCredits(creditsTarget.user_id, amt, creditsNote || "管理员调整");
      toast.success(`已调整 ${amt > 0 ? "+" : ""}${amt} 积分`);
      setCreditsTarget(null);
      setCreditsAmount("");
      setCreditsNote("");
      await load();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const handleResetPassword = async (userId: string) => {
    try {
      const { temp_password } = await resetPassword(userId);
      toast.success(`密码已重置，临时密码：${temp_password}`, { duration: 10000 });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const handleToggleDisable = async (user: AdminUser) => {
    try {
      await updateUser(user.user_id, { disabled: !user.disabled });
      toast.success(user.disabled ? "已启用" : "已禁用");
      await load();
    } catch (e) {
      toast.error(String(e));
    }
  };

  if (loading) return <p className="text-dim">加载中…</p>;
  if (error) return <div className="error-box">{error}</div>;

  return (
    <div>
      <h1 className="page-title">用户管理</h1>
      <p className="page-subtitle">管理所有注册用户、积分余额和账号状态</p>

      <table className="data-table" style={{ marginTop: 24 }}>
        <thead>
          <tr>
            <th>用户名</th>
            <th>积分余额</th>
            <th>角色</th>
            <th>状态</th>
            <th>作品数</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.user_id}>
              <td className="mono">{u.username}</td>
              <td className="mono" style={{ fontWeight: 700 }}>
                <span style={{ color: u.credits_balance <= 0 ? "var(--destructive)" : "inherit" }}>
                  {u.credits_balance}
                </span>
              </td>
              <td>
                {u.is_super_admin && <Badge variant="destructive">超管</Badge>}
                {u.is_admin && !u.is_super_admin && <Badge variant="secondary">管理员</Badge>}
                {!u.is_admin && !u.is_super_admin && <Badge variant="outline">用户</Badge>}
              </td>
              <td>
                {u.disabled ? (
                  <Badge variant="destructive">已禁用</Badge>
                ) : u.credits_balance <= 0 ? (
                  <Badge variant="outline">已冻结</Badge>
                ) : (
                  <Badge variant="completed">正常</Badge>
                )}
              </td>
              <td>{u.workspace_count}</td>
              <td className="text-mute">{fmtTime(u.created_at)}</td>
              <td style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <Button size="sm" variant="outline" onClick={() => setCreditsTarget(u)}>
                  调积分
                </Button>
                <Button size="sm" variant="outline" onClick={() => handleResetPassword(u.user_id)}>
                  重置密码
                </Button>
                <Button
                  size="sm"
                  variant={u.disabled ? "secondary" : "destructive"}
                  onClick={() => handleToggleDisable(u)}
                  disabled={u.is_super_admin}
                >
                  {u.disabled ? "启用" : "禁用"}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <Dialog open={!!creditsTarget} onOpenChange={(o) => !o && setCreditsTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>调整积分 — {creditsTarget?.username}</DialogTitle>
          </DialogHeader>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <Label>调整额度（正=充值，负=扣减）</Label>
              <Input
                type="number"
                value={creditsAmount}
                onChange={(e) => setCreditsAmount(e.target.value)}
                placeholder="如 5000 或 -1000"
                style={{ marginTop: 4 }}
              />
            </div>
            <div>
              <Label>备注</Label>
              <Input
                value={creditsNote}
                onChange={(e) => setCreditsNote(e.target.value)}
                placeholder="调整原因（可选）"
                style={{ marginTop: 4 }}
              />
            </div>
            <p className="text-mute" style={{ fontSize: 13 }}>
              当前余额：{creditsTarget?.credits_balance ?? 0}
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreditsTarget(null)}>
              取消
            </Button>
            <Button onClick={handleAdjustCredits}>确认调整</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}
