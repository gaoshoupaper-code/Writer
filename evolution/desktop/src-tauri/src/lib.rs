// Writer Evolution 桌面端 Rust 后端入口。
//
// 复用写作 desktop 的 Rust 基建层（桌面化改造 2026-07-07）：
// - state:  app 级 reqwest Client 单例 + server_url 管理（cookie jar 自动带 session）
// - http:   http_request / stream_request command（绕 WebView CORS，SSE 中继）
// - config: server_url 读写 command
//
// 删除（写作专用）：cache（离线缓存 workspaces/novels）、updater（绑写作端发布渠道）。
// evolution 桌面端暂不做自动更新，后续如需再加。

mod config;
mod http;
mod state;

use std::sync::Arc;
use state::{AppState, SharedState};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .setup(|app| {
            // 初始化 app 级状态：读 server_url + 建 reqwest Client。
            let handle = app.handle().clone();
            let state =
                tauri::async_runtime::block_on(async move { AppState::init(&handle).await });
            app.manage(Arc::new(state) as SharedState);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            http::http_request,
            http::stream_request,
            config::get_server_url,
            config::set_server_url,
            config::reset_server_url,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
