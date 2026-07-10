import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { fetchAllTransactions, type CreditTransaction } from "@/lib/api";

/**
 * 积分流水（super_admin 专属，只读）。
 *
 * 全局积分变动记录（管理员调整 / 邀请码到账 / 创作消耗 / 预扣退还）。
 * 可切换条数（50/100/200）+ 手动刷新。
 */

const TYPE_LABELS: Record<string, string> = {
  invite_grant: "邀请码到账",
  admin_adjust: "管理员调整",
  creation_consume: "创作消耗",
  creation_refund: "预扣退还",
};

export default function AdminCredits() {
  const [txs, setTxs] = useState<CreditTransaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(100);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setTxs(await fetchAllTransactions(limit));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取流水失败");
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div className="admin-page">
      <header className="page-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <div>
            <h1>积分流水</h1>
            <p className="page-desc">全局积分变动记录（管理员调整 / 邀请码到账 / 创作消耗）</p>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              className="evolve-select"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={50}>最近 50 条</option>
              <option value={100}>最近 100 条</option>
              <option value={200}>最近 200 条</option>
            </select>
            <button className="config-button secondary" onClick={refresh} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
          </div>
        </div>
      </header>

      {loading && txs.length === 0 ? (
        <div className="page-loading">加载中…</div>
      ) : txs.length === 0 ? (
        <div className="config-empty" style={{ padding: 40, textAlign: "center" }}>
          暂无流水记录
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>用户</th>
              <th>类型</th>
              <th>变动</th>
              <th>余额</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            {txs.map((t) => (
              <tr key={t.tx_id}>
                <td className="mono text-mute">{fmtTime(t.created_at)}</td>
                <td className="mono">{t.user_id.slice(0, 8)}…</td>
                <td>
                  <Badge variant={t.amount > 0 ? "completed" : "destructive"}>
                    {TYPE_LABELS[t.type] ?? t.type}
                  </Badge>
                </td>
                <td
                  className="mono"
                  style={{
                    fontWeight: 700,
                    color: t.amount > 0 ? "var(--primary)" : "var(--destructive)",
                  }}
                >
                  {t.amount > 0 ? `+${t.amount}` : t.amount}
                </td>
                <td className="mono">{t.balance_after}</td>
                <td
                  className="text-mute"
                  style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}
                >
                  {t.note ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
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
