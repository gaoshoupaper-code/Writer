"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  fetchInviteCodes,
  createInviteCodes,
  revokeInviteCode,
  type InviteCode,
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

export default function InviteCodesPage() {
  const [codes, setCodes] = useState<InviteCode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [createOpen, setCreateOpen] = useState(false);
  const [count, setCount] = useState("1");
  const [credits, setCredits] = useState("2000");

  const load = useCallback(async () => {
    try {
      setCodes(await fetchInviteCodes());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = async () => {
    const n = parseInt(count, 10);
    const c = parseInt(credits, 10);
    if (isNaN(n) || n < 1) return toast.error("数量无效");
    if (isNaN(c) || c < 0) return toast.error("积分额度无效");
    try {
      const newCodes = await createInviteCodes(n, c);
      toast.success(`已创建 ${newCodes.length} 个邀请码（每个 ${c} 积分）`);
      setCreateOpen(false);
      await load();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const handleRevoke = async (code: string) => {
    if (!confirm(`确定吊销邀请码 ${code.slice(0, 8)}…？`)) return;
    try {
      await revokeInviteCode(code);
      toast.success("已吊销");
      await load();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const handleCopy = (code: string) => {
    navigator.clipboard.writeText(code);
    toast.success("已复制到剪贴板");
  };

  if (loading) return <p className="text-dim">加载中…</p>;
  if (error) return <div className="error-box">{error}</div>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 className="page-title">邀请码管理</h1>
          <p className="page-subtitle">创建带积分配额的邀请码，管理使用状态</p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>创建邀请码</Button>
      </div>

      <table className="data-table" style={{ marginTop: 24 }}>
        <thead>
          <tr>
            <th>邀请码</th>
            <th>积分额度</th>
            <th>状态</th>
            <th>使用者</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {codes.map((c) => (
            <tr key={c.code}>
              <td className="mono" style={{ cursor: "pointer" }} onClick={() => handleCopy(c.code)}>
                {c.code.slice(0, 12)}…
              </td>
              <td className="mono" style={{ fontWeight: 700 }}>
                {c.granted_credits}
              </td>
              <td>
                {c.revoked_at ? (
                  <Badge variant="destructive">已吊销</Badge>
                ) : c.used ? (
                  <Badge variant="completed">已使用</Badge>
                ) : (
                  <Badge variant="secondary">未使用</Badge>
                )}
              </td>
              <td className="text-mute mono">{c.used_by ? c.used_by.slice(0, 8) + "…" : "—"}</td>
              <td className="text-mute">{fmtTime(c.created_at)}</td>
              <td>
                {!c.used && !c.revoked_at && (
                  <Button size="sm" variant="destructive" onClick={() => handleRevoke(c.code)}>
                    吊销
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建邀请码</DialogTitle>
          </DialogHeader>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <Label>数量</Label>
              <Input
                type="number"
                value={count}
                onChange={(e) => setCount(e.target.value)}
                min={1}
                max={50}
                style={{ marginTop: 4 }}
              />
            </div>
            <div>
              <Label>每个邀请码的积分额度</Label>
              <Input
                type="number"
                value={credits}
                onChange={(e) => setCredits(e.target.value)}
                min={0}
                style={{ marginTop: 4 }}
              />
              <p className="text-mute" style={{ fontSize: 13, marginTop: 4 }}>
                用户注册后积分自动到账
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button onClick={handleCreate}>创建</Button>
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
