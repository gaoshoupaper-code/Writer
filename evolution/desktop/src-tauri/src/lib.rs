// Writer Evolution 桌面端 Rust 后端入口。
//
// 复用写作 desktop 的 Rust 基建层（桌面化改造 2026-07-07）：
// - state:  app 级 reqwest Client 单例 + server_url 管理（cookie jar 自动带 session）
// - http:   http_request / stream_request command（绕 WebView CORS，SSE 中继）
// - config: server_url 读写 command
// - updater: 自动更新检查 + 安装（2026-07-08 新增，独立 latest-evo.json 发布渠道）

mod config;
mod http;
mod state;
mod updater;

use std::sync::Arc;
use state::{AppState, SharedState};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            // 初始化 app 级状态：读 server_url + 建 reqwest Client。
            let handle = app.handle().clone();
            let state =
                tauri::async_runtime::block_on(async move { AppState::init(&handle).await });
            app.manage(Arc::new(state) as SharedState);

            // 启动时自动检查更新（只 emit event 提示，不自动装）。
            // 失败静默——更新是锦上添花，不该阻塞启动。
            let update_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                updater::check_on_startup(&update_handle).await;
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            http::http_request,
            http::stream_request,
            config::get_server_url,
            config::set_server_url,
            config::reset_server_url,
            updater::check_update,
            updater::install_update,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
