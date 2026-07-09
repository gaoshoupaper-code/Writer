"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { fetchAllTransactions, type CreditTransaction } from "@/lib/admin-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const TYPE_LABELS: Record<string, string> = {
  invite_grant: "邀请码到账",
  admin_adjust: "管理员调整",
  creation_consume: "创作消耗",
  creation_refund: "预扣退还",
};

export default function CreditsPage() {
  const [txs, setTxs] = useState<CreditTransaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [limit, setLimit] = useState(100);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setTxs(await fetchAllTransactions(limit));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && txs.length === 0) return <p className="text-dim">加载中…</p>;
  if (error) return <div className="error-box">{error}</div>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 className="page-title">积分流水</h1>
          <p className="page-subtitle">全局积分变动记录（管理员调整 / 邀请码到账 / 创作消耗）</p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value, 10))}
            className="border border-border rounded-xl px-3 py-1.5 text-sm bg-transparent"
          >
            <option value={50}>最近 50 条</option>
            <option value={100}>最近 100 条</option>
            <option value={200}>最近 200 条</option>
          </select>
          <Button variant="outline" onClick={load}>
            刷新
          </Button>
        </div>
      </div>

      <table className="data-table" style={{ marginTop: 24 }}>
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
              <td className="text-mute">{fmtTime(t.created_at)}</td>
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
                {t.amount > 0 ? "+" : ""}
                {t.amount}
              </td>
              <td className="mono">{t.balance_after}</td>
              <td className="text-mute" style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>
                {t.note ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {txs.length === 0 && <p className="text-dim" style={{ textAlign: "center", padding: 40 }}>暂无流水记录</p>}
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
