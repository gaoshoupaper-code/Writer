"use client";

/**
 * 启动进化对话框（D6 可调参数）。
 *
 * rounds   / patience / judge_j 三个参数，带默认值 + 校验。
 * 成功后跳转到对应 session 驾驶舱（router.push）。
 * 失败则内联报错，不关闭对话框。
 *
 * 视觉：沿用 dialog 基类（Swiss grid），参数用 mono 数字输入。
 */
import { useRouter } from "next/navigation";
import { useState } from "react";
import { startAdapt } from "@/lib/adapt-api";
import type { AdaptStartParams } from "@/lib/adapt-types";

const DEFAULTS: AdaptStartParams = { rounds: 3, patience: 2, judge_j: 3 };

export function StartAdaptDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [params, setParams] = useState<AdaptStartParams>(DEFAULTS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (key: keyof AdaptStartParams, value: number) =>
    setParams((p) => ({ ...p, [key]: value }));

  const handleStart = async () => {
    setBusy(true);
    setError(null);
    try {
      const resp = await startAdapt(params);
      onClose();
      // 跳转到驾驶舱实时观察
      router.push(`/sessions/?id=${resp.session_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "启动失败");
      setBusy(false);
    }
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div
        className="dialog-panel node-appear"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="dialog-header">
          <h3>启动进化循环</h3>
          <button className="dialog-close" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>

        <p className="dialog-intro">
          以当前 production 配置为基准，跑一轮 AEGIS adapt loop。
          参数决定循环的预算与收敛策略。
        </p>

        <div className="param-grid">
          <ParamField
            label="rounds"
            hint="最大轮次 T。每轮跑 baseline + K 个候选，耗时长。"
            value={params.rounds}
            min={1}
            max={10}
            onChange={(v) => set("rounds", v)}
          />
          <ParamField
            label="patience"
            hint="连续无改善的退出阈值 P。patience=2 表示连续 2 轮没涨就停。"
            value={params.patience}
            min={1}
            max={5}
            onChange={(v) => set("patience", v)}
          />
          <ParamField
            label="judge_j"
            hint="verifier 打分次数。越大越稳但越慢。"
            value={params.judge_j}
            min={1}
            max={5}
            onChange={(v) => set("judge_j", v)}
          />
        </div>

        {error && <div className="error-box" style={{ marginTop: 14 }}>{error}</div>}

        <div className="dialog-actions">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn-primary" onClick={handleStart} disabled={busy}>
            {busy ? "启动中…" : "启动"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ParamField({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="param-field">
      <label className="param-label mono">{label}</label>
      <div className="param-control">
        <input
          type="range"
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="param-range"
        />
        <span className="param-value mono">{value}</span>
      </div>
      <span className="param-hint">{hint}</span>
    </div>
  );
}
