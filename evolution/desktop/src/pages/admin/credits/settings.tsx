import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  fetchCreditsConfig,
  updateCreditsConfig,
  type CreditConfigItem,
} from "@/lib/api";

/**
 * 积分参数设置（super_admin 专属，暗调旋钮）。
 *
 * 后端返回 config map（key → {value, description, updated_at}），前端逐项渲染卡片。
 * 修改即时生效（后端热加载，不重启）。dirty 时保存按钮高亮。
 */

const KEY_LABELS: Record<string, string> = {
  output_token_weight: "输出 token 权重",
  input_miss_weight: "输入未命中权重",
  input_hit_weight: "输入缓存命中权重",
  credits_per_1k_tokens: "积分/千标准token（主旋钮）",
  tier_hold_amounts: "六档预扣额度（JSON）",
  max_debt: "最大负债上限（触及强停）",
};

export default function AdminCreditsSettings() {
  const [config, setConfig] = useState<Record<string, CreditConfigItem>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchCreditsConfig();
      setConfig(data);
      // 初始化 edit buffer：每项 = 当前值
      setEdits(Object.fromEntries(Object.entries(data).map(([k, v]) => [k, v.value])));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取积分配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleSave(key: string) {
    const value = edits[key];
    if (value === undefined || value === config[key].value) return;
    setSavingKey(key);
    try {
      await updateCreditsConfig(key, value);
      toast.success(`${KEY_LABELS[key] ?? key} 已更新`);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSavingKey(null);
    }
  }

  if (loading) {
    return <div className="page-loading">加载中…</div>;
  }

  return (
    <div className="admin-page">
      <header className="page-header">
        <h1>积分参数设置</h1>
        <p className="page-desc">暗调旋钮 — 修改后即时生效（不重启），用户无感知</p>
      </header>

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {Object.entries(config).map(([key, item]) => {
          const dirty = edits[key] !== item.value;
          return (
            <div
              key={key}
              style={{
                border: "1px solid var(--border)",
                borderRadius: 12,
                padding: 16,
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                }}
              >
                <span style={{ fontWeight: 700 }}>{KEY_LABELS[key] ?? key}</span>
                <code style={{ fontSize: 12, color: "var(--muted-foreground)" }}>{key}</code>
              </div>
              <p
                style={{
                  fontSize: 13,
                  color: "var(--muted-foreground)",
                  margin: "8px 0 12px",
                }}
              >
                {item.description}
              </p>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  className="config-input mono"
                  value={edits[key] ?? ""}
                  onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
                  disabled={savingKey === key}
                  style={{ flex: 1 }}
                />
                <button
                  className={`config-button small ${dirty ? "primary" : "secondary"}`}
                  onClick={() => handleSave(key)}
                  disabled={!dirty || savingKey === key}
                >
                  {savingKey === key ? "保存中…" : "保存"}
                </button>
              </div>
              <div
                style={{
                  fontSize: 12,
                  color: "var(--muted-foreground)",
                  marginTop: 8,
                }}
              >
                上次更新：{fmtTime(item.updated_at)}
              </div>
            </div>
          );
        })}
      </div>

      <div
        style={{
          background: "var(--accent)",
          borderRadius: 12,
          padding: 16,
          marginTop: 24,
          color: "var(--muted-foreground)",
          fontSize: 13,
          lineHeight: 1.7,
        }}
      >
        <strong>暗调说明：</strong>
        修改「积分/千标准token」是最隐蔽的暗调方式——token 消耗本身每次波动，
        用户无法区分是 token 增多了还是单价涨了。建议每次调整幅度 &lt;10%。
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
