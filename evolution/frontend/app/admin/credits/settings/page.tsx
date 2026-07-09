"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { fetchCreditsConfig, updateCreditsConfig, type CreditConfigItem } from "@/lib/admin-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const KEY_LABELS: Record<string, string> = {
  output_token_weight: "输出 token 权重",
  input_miss_weight: "输入未命中权重",
  input_hit_weight: "输入缓存命中权重",
  credits_per_1k_tokens: "积分/千标准token（主旋钮）",
  tier_hold_amounts: "六档预扣额度（JSON）",
  max_debt: "最大负债上限（触及强停）",
};

export default function CreditsSettingsPage() {
  const [config, setConfig] = useState<Record<string, CreditConfigItem>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const data = await fetchCreditsConfig();
      setConfig(data);
      setEdits(Object.fromEntries(Object.entries(data).map(([k, v]) => [k, v.value])));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleSave = async (key: string) => {
    const value = edits[key];
    if (value === undefined || value === config[key]?.value) return;
    try {
      await updateCreditsConfig(key, value);
      toast.success(`${KEY_LABELS[key] ?? key} 已更新`);
      await load();
    } catch (e) {
      toast.error(String(e));
    }
  };

  if (loading) return <p className="text-dim">加载中…</p>;
  if (error) return <div className="error-box">{error}</div>;

  return (
    <div>
      <h1 className="page-title">积分参数设置</h1>
      <p className="page-subtitle">暗调旋钮 — 修改后即时生效（不重启），用户无感知</p>

      <div style={{ marginTop: 24, display: "flex", flexDirection: "column", gap: 20 }}>
        {Object.entries(config).map(([key, item]) => (
          <div
            key={key}
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              padding: 16,
              border: "1px solid var(--border)",
              borderRadius: 12,
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontWeight: 700 }}>{KEY_LABELS[key] ?? key}</span>
              <code className="text-mute" style={{ fontSize: 12 }}>
                {key}
              </code>
            </div>
            <p className="text-mute" style={{ fontSize: 13 }}>
              {item.description}
            </p>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
              <Input
                value={edits[key] ?? ""}
                onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
                className="mono"
                style={{ flex: 1 }}
              />
              <Button
                size="sm"
                variant={edits[key] !== item.value ? "default" : "outline"}
                onClick={() => handleSave(key)}
                disabled={edits[key] === item.value}
              >
                保存
              </Button>
            </div>
            <p className="text-mute" style={{ fontSize: 12 }}>
              上次更新：{fmtTime(item.updated_at)}
            </p>
          </div>
        ))}
      </div>

      <div
        style={{
          marginTop: 24,
          padding: 16,
          background: "var(--accent)",
          borderRadius: 12,
          fontSize: 13,
          color: "var(--muted-foreground)",
        }}
      >
        <strong>暗调说明：</strong>
        修改"积分/千标准token"是最隐蔽的暗调方式——token 消耗本身每次波动，用户无法区分是
        token 增多了还是单价涨了。建议每次调整幅度 &lt;10%。
      </div>
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
