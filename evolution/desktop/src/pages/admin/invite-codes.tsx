import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetClose,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import {
  fetchInviteCodes,
  createInviteCodes,
  revokeInviteCode,
  type InviteCode,
} from "@/lib/api";

/**
 * 邀请码管理（super_admin 专属）。
 *
 * - 创建：批量生成带积分配额的邀请码（数量 1-50，每码积分额度）
 * - 吊销：confirm 确认后 revoke
 * - 复制：点击 code 单元格复制完整码到剪贴板
 */
export default function AdminInviteCodes() {
  const [codes, setCodes] = useState<InviteCode[]>([]);
  const [loading, setLoading] = useState(true);

  // 创建 Sheet 状态
  const [createOpen, setCreateOpen] = useState(false);
  const [count, setCount] = useState("1");
  const [credits, setCredits] = useState("2000");
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setCodes(await fetchInviteCodes());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取邀请码失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCreate() {
    const n = parseInt(count, 10);
    const c = parseInt(credits, 10);
    if (isNaN(n) || n < 1) {
      toast.error("数量无效（1-50）");
      return;
    }
    if (isNaN(c) || c < 0) {
      toast.error("积分额度无效（≥0）");
      return;
    }
    setSubmitting(true);
    try {
      const newCodes = await createInviteCodes(n, c);
      toast.success(`已创建 ${newCodes.length} 个邀请码（每个 ${c} 积分）`);
      setCreateOpen(false);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "创建邀请码失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRevoke(code: InviteCode) {
    if (!confirm(`确定吊销邀请码 ${code.code.slice(0, 8)}…？此操作不可撤销。`)) return;
    try {
      await revokeInviteCode(code.code);
      toast.success("已吊销");
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "吊销失败");
    }
  }

  async function handleCopy(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败");
    }
  }

  if (loading) {
    return <div className="page-loading">加载中…</div>;
  }

  return (
    <div className="admin-page">
      <header className="page-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <div>
            <h1>邀请码管理</h1>
            <p className="page-desc">创建带积分配额的邀请码，管理使用状态</p>
          </div>
          <button className="config-button primary" onClick={() => setCreateOpen(true)}>
            + 创建邀请码
          </button>
        </div>
      </header>

      <table className="data-table">
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
              <td
                className="mono"
                style={{ cursor: "pointer" }}
                onClick={() => handleCopy(c.code)}
                title="点击复制完整邀请码"
              >
                {c.code.slice(0, 12)}…
              </td>
              <td className="mono" style={{ fontWeight: 700 }}>{c.granted_credits}</td>
              <td>
                {c.revoked_at ? (
                  <Badge variant="destructive">已吊销</Badge>
                ) : c.used ? (
                  <Badge variant="completed">已使用</Badge>
                ) : (
                  <Badge variant="secondary">未使用</Badge>
                )}
              </td>
              <td className="mono text-mute">
                {c.used_by ? `${c.used_by.slice(0, 8)}…` : "—"}
              </td>
              <td className="mono text-mute">{fmtTime(c.created_at)}</td>
              <td>
                {!c.used && !c.revoked_at && (
                  <button
                    className="config-button small danger"
                    onClick={() => handleRevoke(c)}
                  >
                    吊销
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {codes.length === 0 && <div className="config-empty">暂无邀请码</div>}

      {/* 创建邀请码 Sheet */}
      <Sheet open={createOpen} onOpenChange={(o) => !submitting && setCreateOpen(o)}>
        <SheetContent side="right">
          <SheetHeader>
            <SheetTitle>创建邀请码</SheetTitle>
            <SheetClose asChild>
              <button className="sheet-close-x">✕</button>
            </SheetClose>
          </SheetHeader>
          <div className="sheet-body">
            <label className="config-field">
              <span className="config-label">数量（1-50）</span>
              <input
                className="config-input"
                type="number"
                min={1}
                max={50}
                value={count}
                onChange={(e) => setCount(e.target.value)}
                disabled={submitting}
              />
            </label>
            <label className="config-field">
              <span className="config-label">每个邀请码的积分额度</span>
              <input
                className="config-input"
                type="number"
                min={0}
                value={credits}
                onChange={(e) => setCredits(e.target.value)}
                disabled={submitting}
              />
              <span className="config-hint">用户注册后积分自动到账</span>
            </label>
            <div className="config-actions">
              <button
                className="config-button primary"
                onClick={handleCreate}
                disabled={submitting}
              >
                {submitting ? "创建中…" : "创建"}
              </button>
              <button
                className="config-button ghost"
                onClick={() => setCreateOpen(false)}
                disabled={submitting}
              >
                取消
              </button>
            </div>
          </div>
        </SheetContent>
      </Sheet>
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
