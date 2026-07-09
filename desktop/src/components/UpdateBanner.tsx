import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { toast } from "sonner";

/**
 * 更新提示横条（自动更新，2026-07-09 新增）。
 *
 * 与进化端共用同一套后端契约（updater.rs 的 check_update / install_update）：
 * - 启动时 Rust 后端自动检查更新并 emit "update_available" event；
 * - 本组件 listen 该 event，有新版时在窗口顶部（fixed 钉顶）显示提示条；
 * - 用户点「更新内容 ▾」可展开看本次版本的更新条目（changelog，来自 latest.json 的 notes）；
 * - 用户点「立即更新」→ 下载 + 验签 + 安装 + 重启。
 *
 * 与进化端的差异：进化端把 banner 挂在 Shell 内（inline 流式），写作端没有统一
 * Shell（App.tsx 直接路由各页），所以这里用 position:fixed 钉顶，保证 login/
 * 工作区/settings 等所有页面都能看到提示。
 *
 * 检查失败静默——更新是锦上添花，不打扰用户。
 */

/** Rust updater::UpdateInfo 的前端镜像（字段对齐，可选字段用 null 兜底）。 */
interface UpdateInfo {
  available: boolean;
  status: string;
  current_version: string;
  version: string | null;
  date: string | null;
  body: string | null;
}

/**
 * 解析 notes（body 字段）为更新条目列表。
 * 发布脚本把 changelog 写成 JSON 数组字符串（如 '["修复A","新增B"]'），
 * 兼容旧版的纯文本（按换行拆分）和空值。
 */
function parseChangelog(body: string | null): string[] {
  if (!body) return [];
  // 优先尝试 JSON 数组解析（新版发布脚本格式）
  try {
    const parsed = JSON.parse(body);
    if (Array.isArray(parsed) && parsed.every((x) => typeof x === "string")) {
      return parsed;
    }
  } catch {
    // 不是 JSON，按纯文本处理
  }
  // 兼容旧版/纯文本：按换行拆分，去空行去重
  return body
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export default function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [installing, setInstalling] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    // 双保险拉更新信息：
    //   1. listen 后端启动检查 emit 的 event（check_on_startup 发的）
    //   2. 挂载后主动 invoke 一次 —— 兜底 event 竞态
    // 后端 check_on_startup 在 setup 里 spawn，可能在前端 listen() 注册好之前
    // 就 emit 了 event，瞬态 event 无 listener 即丢失。主动 invoke 走返回值不走 event，
    // 确保组件挂载后一定能拿到结果。
    let unlisten: UnlistenFn | undefined;
    listen<UpdateInfo>("update_available", (event) => {
      if (event.payload?.available) {
        setInfo(event.payload);
      }
    }).then((fn) => {
      unlisten = fn;
    });

    // 主动拉一次兜底（与 event 互不冲突：setInfo 幂等，都是 available 才设）
    invoke<UpdateInfo>("check_update")
      .then((info) => {
        if (info.available) setInfo(info);
      })
      .catch(() => {
        // 检查失败静默（更新是锦上添花）
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

  const changelog = parseChangelog(info.body);
  const hasChangelog = changelog.length > 0;

  return (
    <div className="update-banner">
      <div className="update-banner-main">
        <span className="update-banner-text">
          ✨ 发现新版本 v{info.version}
          <span className="update-banner-note">（当前 v{info.current_version}）</span>
        </span>
        {hasChangelog && (
          <button
            className="update-banner-toggle"
            onClick={() => setExpanded((v) => !v)}
          >
            更新内容 {expanded ? "▴" : "▾"}
          </button>
        )}
      </div>
      <button
        className="update-banner-btn"
        onClick={handleInstall}
        disabled={installing}
      >
        {installing ? "下载安装中…" : "立即更新"}
      </button>
      {expanded && hasChangelog && (
        <ul className="update-banner-changelog">
          {changelog.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
