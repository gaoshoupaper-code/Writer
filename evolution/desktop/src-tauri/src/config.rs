//! 服务器地址配置 command（设计文档 S6/T8）。
//!
//! 前端通过这两个 command 读写 server_url。
//! 设置页改地址 → set_server_url → 触发 Client 重建 + cookie 清空（见 state.rs）。

use crate::state::{save_server_url, SharedState, DEFAULT_SERVER_URL};
use tauri::{AppHandle, State};

/// 读当前 server_url。
/// 前端用法：`const url = await invoke("get_server_url")`
#[tauri::command]
pub async fn get_server_url(state: State<'_, SharedState>) -> Result<String, String> {
    Ok(state.server_url().await)
}

/// 写 server_url（持久化到 Store + 重建 Client）。
/// 前端用法：`await invoke("set_server_url", { url: "https://..." })`
///
/// 返回更新后的 URL（规范化后），前端用它刷新显示。
#[tauri::command]
pub async fn set_server_url(
    app: AppHandle,
    state: State<'_, SharedState>,
    url: String,
) -> Result<String, String> {
    // 先持久化（失败就报错，不重建 Client）
    save_server_url(&app, &url).await?;
    // 持久化成功 → 重建 Client + 清 cookie
    state.set_server_url(url).await;
    // 返回规范化后的 URL（set_server_url 内部已规范化，重新读出来）
    Ok(state.server_url().await)
}

/// 重置为默认服务器地址。
#[tauri::command]
pub async fn reset_server_url(
    app: AppHandle,
    state: State<'_, SharedState>,
) -> Result<String, String> {
    save_server_url(&app, DEFAULT_SERVER_URL).await?;
    state.set_server_url(DEFAULT_SERVER_URL.to_string()).await;
    Ok(state.server_url().await)
}
