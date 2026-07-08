import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { toast } from "sonner";

/**
 * 更新提示横条（2026-07-08 新增）。
 *
 * 启动时 Rust 后端自动检查更新并 emit "update_available" event；
 * 本组件 listen 该 event，有新版时在顶部显示提示条，用户点「立即更新」后
 * 下载 + 验签 + 安装 + 重启。
 *
 * 检查失败静默——更新是锦上添花，不打扰用户。
 */

/** Rust updater::UpdateInfo 的前端镜像（字段对齐，可选字段用 null 兜底）。 */
interface UpdateInfo {
  available: boolean;
  current_version: string;
  version: string | null;
  date: string | null;
  body: string | null;
}

export default function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [installing, setInstalling] = useState(false);

  useEffect(() => {
    // listen 后端启动检查 emit 的 event
    let unlisten: UnlistenFn | undefined;
    listen<UpdateInfo>("update_available", (event) => {
      if (event.payload?.available) {
        setInfo(event.payload);
      }
    }).then((fn) => {
      unlisten = fn;
    });

    return () => {
      unlisten?.();
    };
  }, []);

  async function handleInstall() {
    if (installing) return;
    if (!confirm("确认现在下载并安装更新？安装完成后应用将自动重启。")) return;
    setInstalling(true);
    try {
      await invoke("install_update");
      // install_update 内部会 app.restart()，这行正常情况执行不到
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "更新安装失败");
      setInstalling(false);
    }
  }

  if (!info?.available) return null;

  return (
    <div className="update-banner">
      <span className="update-banner-text">
        ✨ 发现新版本 v{info.version}
        {info.body && <span className="update-banner-note">（当前 v{info.current_version}）</span>}
      </span>
      <button
        className="update-banner-btn"
        onClick={handleInstall}
        disabled={installing}
      >
        {installing ? "下载安装中…" : "立即更新"}
      </button>
    </div>
  );
}
