import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { toast } from "sonner";

/**
 * App 更新检查（从原 config.tsx 抽出，scope 分家 2026-07-18）。
 *
 * 与 LLM 配置无关，是 App 自身的版本检查功能。D22 决策：留在"进化端模型"壳页面，
 * 维持"在配置页里能检查更新"的用户认知。
 */
export default function AppUpdateCheck() {
  const [checking, setChecking] = useState(false);

  async function handleCheckUpdate() {
    setChecking(true);
    try {
      const info = await invoke<{
        available: boolean;
        status: string;
        current_version: string;
        version: string | null;
        body: string | null;
      }>("check_update");
      // 凭 status 精确区分「已是最新」与「检查失败」，避免谎报"已是最新"。
      if (info.status === "update_available") {
        toast.success(`发现新版本 v${info.version}，请看顶部提示更新`);
      } else if (info.status === "check_failed") {
        toast.error(info.body ?? "检查更新失败");
      } else {
        toast.success(`已是最新版本（v${info.current_version}）`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "检查更新失败");
    } finally {
      setChecking(false);
    }
  }

  return (
    <section className="config-update-section">
      <div className="config-update-info">
        <span className="config-label">应用更新</span>
        <span className="config-hint">检查是否有新版本，有则可在顶部提示条一键更新</span>
      </div>
      <button
        className="config-button secondary"
        onClick={handleCheckUpdate}
        disabled={checking}
      >
        {checking ? "检查中…" : "检查更新"}
      </button>
    </section>
  );
}
